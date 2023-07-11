#!/usr/bin/env python3

import asyncio
import http
import http.client
import json
import logging
import os
import re
import socket
import time
from concurrent import futures
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import machineid
import psutil
from bs4 import BeautifulSoup
from commonwealth.utils.apis import GenericErrorHandlingRoute, PrettyJSONResponse
from commonwealth.utils.decorators import temporary_cache
from commonwealth.utils.logs import InterceptHandler, init_logger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi_versioning import VersionedFastAPI, version
from pydantic import BaseModel
from speedtest import Speedtest
from uvicorn import Config, Server

BLUEOS_VERSION = os.environ.get("GIT_DESCRIBE_TAGS", "null")
HTML_FOLDER = Path.joinpath(Path(__file__).parent.absolute(), "html")
SPEED_TEST: Optional[Speedtest] = None


logging.basicConfig(handlers=[InterceptHandler()], level=logging.DEBUG)
init_logger("Helper")

try:
    SPEED_TEST = Speedtest(secure=True)
except Exception:
    # When starting, the system may not be connected to the internet
    pass


class Website(Enum):
    ArduPilot = {
        "hostname": "firmware.ardupilot.org",
        "path": "/",
        "port": 80,
    }
    AWS = {
        "hostname": "amazon.com",
        "path": "/",
        "port": 80,
    }
    BlueOS = {
        "hostname": "blueos.cloud",
        "path": f"/ping?id={machineid.hashed_id()}&version={BLUEOS_VERSION}",
        "port": 80,
    }
    Cloudflare = {
        "hostname": "1.1.1.1",
        "path": "/",
        "port": 80,
    }
    GitHub = {
        "hostname": "github.com",
        "path": "/",
        "port": 80,
    }


class WebsiteStatus(BaseModel):
    site: Website
    online: bool
    error: Optional[str] = None


class ServiceMetadata(BaseModel):
    name: str
    description: str
    icon: str
    company: str
    version: str
    webpage: str
    route: Optional[str]
    new_page: Optional[bool]
    api: str
    sanitized_name: Optional[str]


class ServiceInfo(BaseModel):
    valid: bool
    title: str
    documentation_url: str
    versions: List[str]
    port: int
    metadata: Optional[ServiceMetadata]


class SpeedtestServer(BaseModel):
    url: str
    lat: str
    lon: str
    name: str
    country: str
    cc: str
    sponsor: str
    id: str
    host: str
    d: float
    latency: float


class SpeedtestClient(BaseModel):
    ip: str
    lat: str
    lon: str
    isp: str
    isprating: str
    rating: str
    ispdlavg: str
    ispulavg: str
    loggedin: str
    country: str


class SpeedTestResult(BaseModel):
    download: float
    upload: float
    ping: float
    server: SpeedtestServer
    timestamp: datetime
    bytes_sent: int
    bytes_received: int
    share: Optional[str] = None
    client: SpeedtestClient


class SimpleHttpResponse(BaseModel):
    status: Optional[int]
    decoded_data: Optional[str]
    as_json: Optional[Union[List[Any], Dict[Any, Any]]]
    error: Optional[str]


class Helper:
    LOCALSERVER_CANDIDATES = ["0.0.0.0", "::"]
    DOCS_CANDIDATE_URLS = ["/docs", "/v1.0/ui/"]
    API_CANDIDATE_URLS = ["/docs.json", "/openapi.json", "/swagger.json"]
    PORT = 81
    KNOWN_BLUEOS_PORTS = [
        PORT,  # Helper (itself)
        2748,  # NMEA Injector
        6020,  # MAVLink Camera Manager
        6030,  # System Information
        6040,  # MAVLink2Rest
        7777,  # File Browser
        8000,  # ArduPilot Manager
        8081,  # Version Chooser
        8088,  # ttyd
        9000,  # Wifi Manager
        9002,  # Cerulean DVL
        9090,  # Cable-guy
        9100,  # Commander
        9101,  # Bag Of Holding
        9110,  # Ping Service
        9111,  # Beacon Service
        9120,  # Pardal
        9134,  # Kraken
        27353,  # Bridget
    ]
    SKIP_PORTS = [
        22,  # SSH
        80,  # BlueOS
        6021,  # Mavlink Camera Manager's WebRTC signaller
        8554,  # Mavlink Camera Manager's RTSP server
        5777,  # ardupilot-manager's Mavlink TCP Server
        5555,  # DGB server
        2770,  # NGINX
    ]
    FOUND_BLUEOS_SERVICES: Dict[int, ServiceInfo] = {}

    @staticmethod
    # pylint: disable=too-many-arguments
    def simple_http_request(
        host: str,
        port: int = http.client.HTTP_PORT,
        path: str = "/",
        timeout: Optional[float] = None,
        method: str = "GET",
        try_json: bool = False,
    ) -> SimpleHttpResponse:
        """This function is a simple wrappper around http.client to make convenient requests and get the answer
        knowing that it will never raise"""
        conn = None
        request_response = SimpleHttpResponse(status=None, decoded_data=None, as_json=None, timeout=False, error=None)
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

            headers = {}
            if try_json:
                headers["Accept"] = "application/json"
            conn.request(method, path, headers=headers)
            response = conn.getresponse()

            request_response.status = response.status
            if response.status == http.client.OK:
                encoding = response.headers.get_content_charset() or "utf-8"
                request_response.decoded_data = response.read().decode(encoding)

                if try_json:
                    request_response.as_json = json.loads(request_response.decoded_data)

        except (http.client.HTTPException, socket.error, json.JSONDecodeError) as e:
            logging.warning(e)
            request_response.error = str(e)

        except Exception as e:
            logging.error(e, exc_info=True)
            request_response.error = str(e)

        finally:
            if conn:
                conn.close()

        return request_response

    @staticmethod
    @temporary_cache(timeout_seconds=60)  # a temporary cache helps us deal with changes in metadata
    # pylint: disable=too-many-branches
    def detect_service(port: int) -> ServiceInfo:
        # Use our ethernal cache of BlueOS services
        if port in Helper.FOUND_BLUEOS_SERVICES:
            return Helper.FOUND_BLUEOS_SERVICES[port]

        log_msg = f"Detecting service at port {port}"
        start_time = time.process_time()
        info = ServiceInfo(valid=False, title="Unknown", documentation_url="", versions=[], port=port)

        response = Helper.simple_http_request("127.0.0.1", port=port, path="/", timeout=0.2, method="GET")
        if response.status == http.client.OK:
            info.valid = True
            try:
                soup = BeautifulSoup(response.decoded_data, features="html.parser")
                title_element = soup.find("title")
                info.title = title_element.text if title_element else "Unknown"
            except Exception as e:
                logging.warning(f"Failed parsing the service title: {e}")

        # If not valid web server, documentation will not be available
        if not info.valid:
            logging.debug(f"{log_msg}: invalid. Took: {time.process_time() - start_time} s")
            return info

        # Check for service description metadata
        response = Helper.simple_http_request(
            "127.0.0.1", port=port, path="/register_service", timeout=0.2, method="GET", try_json=True
        )
        response_as_json = response.as_json
        if response.status == http.client.OK and response_as_json is not None and isinstance(response_as_json, dict):
            try:
                info.metadata = ServiceMetadata.parse_obj(response_as_json)
                info.metadata.sanitized_name = re.sub(r"[^a-z0-9]", "", info.metadata.name.lower())
            except Exception as e:
                logging.warning(f"Failed parsing the received JSON as ServiceMetadata object: {e}")
        else:
            logging.debug(f"No metadata received from {info.title} (port {port})")

        for documentation_path in Helper.DOCS_CANDIDATE_URLS:
            try:
                # Skip until we find a valid documentation path
                response = Helper.simple_http_request(
                    "127.0.0.1", port=port, path=documentation_path, timeout=0.2, method="GET"
                )
                if response.status != http.client.OK:
                    continue
                info.documentation_url = documentation_path

                # Get main openapi json description file
                for api_path in Helper.API_CANDIDATE_URLS:
                    response = Helper.simple_http_request(
                        "127.0.0.1", port=port, path=api_path, timeout=0.2, method="GET", try_json=True
                    )

                    # Skip until we find the expected data
                    response_as_json = response.as_json
                    if (
                        response.status != http.client.OK
                        or response_as_json is None
                        or not isinstance(response_as_json, dict)
                        or "paths" not in response_as_json
                        or not isinstance(response_as_json["paths"], dict)
                    ):
                        continue

                    # Check all available versions for the ones that provide a swagger-ui
                    for path in response_as_json["paths"].keys():
                        response = Helper.simple_http_request(
                            "127.0.0.1", port=port, path=path, timeout=0.2, method="GET"
                        )
                        if (
                            response.status == http.client.OK
                            and response.decoded_data is not None
                            and "swagger-ui" in response.decoded_data
                        ):
                            info.versions += [path]

                break

            except Exception as e:
                # This should be avoided by the first try block, but better safe than sorry
                logging.error(e, exc_info=True)
                continue

        # Add the BlueOS services to an ethernal cache
        if info.valid and port in Helper.KNOWN_BLUEOS_PORTS:
            Helper.FOUND_BLUEOS_SERVICES[port] = info

        logging.debug(f"{log_msg}: valid. Took: {time.process_time() - start_time} s")
        return info

    @staticmethod
    @temporary_cache(timeout_seconds=10)
    def scan_ports() -> List[ServiceInfo]:
        log_msg = "Running scan_ports"
        start_time = time.process_time()

        # Filter for TCP ports that are listen and can be accessed by external users (server in 0.0.0.0)
        connections = (
            connection
            for connection in psutil.net_connections("tcp")
            if connection.status == psutil.CONN_LISTEN and connection.laddr.ip in Helper.LOCALSERVER_CANDIDATES
        )

        # And check if there is a webpage available that is not us
        # Use it as a set to remove duplicated ports
        ports = set(connection.laddr.port for connection in connections)
        services = [Helper.detect_service(port) for port in ports if port not in Helper.SKIP_PORTS]
        ret = [service for service in services if service.valid]

        logging.debug(f"{log_msg}. Took: {time.process_time() - start_time} s")
        return ret

    @staticmethod
    @temporary_cache(timeout_seconds=1)
    def check_website(site: Website) -> WebsiteStatus:
        start_time = time.process_time()

        hostname = str(site.value["hostname"])
        port = int(str(site.value["port"]))
        path = str(site.value["path"])

        response = Helper.simple_http_request(hostname, port=port, path=path, timeout=10, method="GET")
        website_status = WebsiteStatus(site=site, online=False)

        log_msg = f"Running check_website for '{hostname}:{port}'"
        if response.error is None:
            logging.debug(f"{log_msg}: Online. Took: {time.process_time() - start_time} s")
            website_status.online = True
        else:
            logging.warning(
                f"{log_msg}: Offline. Error occurred: {website_status.error}. Took: {time.process_time() - start_time} s"
            )
            website_status.error = response.error

        return website_status

    @staticmethod
    @temporary_cache(timeout_seconds=1)
    def check_internet_access() -> Dict[str, WebsiteStatus]:
        log_msg = "Running check_internet_access"
        start_time = time.process_time()

        with futures.ThreadPoolExecutor() as executor:
            tasks = [executor.submit(Helper.check_website, site) for site in Website]
            status_list = [task.result() for task in futures.as_completed(tasks)]

        ret = {status.site.name: status for status in status_list}

        logging.debug(f"{log_msg}. Took: {time.process_time() - start_time} s")
        return ret


fast_api_app = FastAPI(
    title="Helper API",
    description="Everybody's helper to find web services that are running in BlueOS.",
    default_response_class=PrettyJSONResponse,
)
fast_api_app.router.route_class = GenericErrorHandlingRoute


@fast_api_app.get(
    "/web_services",
    response_model=List[ServiceInfo],
    summary="Retrieve web services found.",
)
@version(1, 0)
def web_services() -> Any:
    """REST API endpoint to retrieve web services running."""
    return Helper.scan_ports()


@fast_api_app.get(
    "/check_internet_access",
    response_model=Dict[str, WebsiteStatus],
    summary="Used to check if some websites are available or if there is internet access.",
)
@version(1, 0)
def check_internet_access() -> Any:
    return Helper.check_internet_access()


@fast_api_app.get(
    "/internet_best_server",
    response_model=SpeedTestResult,
    summary="Check internet best server for test from BlueOS.",
)
@version(1, 0)
async def internet_best_server() -> Any:
    # Since we are finding a new server, clear previous results
    # pylint: disable=global-statement
    global SPEED_TEST
    SPEED_TEST = Speedtest(secure=True)
    SPEED_TEST.get_best_server()
    return SPEED_TEST.results.dict()


@fast_api_app.get(
    "/internet_download_speed",
    response_model=SpeedTestResult,
    summary="Check internet download speed test from BlueOS.",
)
@version(1, 0)
async def internet_download_speed() -> Any:
    if not SPEED_TEST:
        raise RuntimeError("SPEED_TEST not initialized, initialize server search.")
    SPEED_TEST.download()
    return SPEED_TEST.results.dict()


@fast_api_app.get(
    "/internet_upload_speed",
    response_model=SpeedTestResult,
    summary="Check internet upload speed test from BlueOS.",
)
@version(1, 0)
async def internet_upload_speed() -> Any:
    if not SPEED_TEST:
        raise RuntimeError("SPEED_TEST not initialized, initialize server search.")
    SPEED_TEST.upload()
    return SPEED_TEST.results.dict()


@fast_api_app.get(
    "/internet_test_previous_result",
    response_model=SpeedTestResult,
    summary="Return previous result of internet speed test.",
)
@version(1, 0)
async def internet_test_previous_result() -> Any:
    if not SPEED_TEST:
        raise RuntimeError("SPEED_TEST not initialized, initialize server search.")
    return SPEED_TEST.results.dict()


async def periodic() -> None:
    while True:
        Helper.check_internet_access()
        Helper.scan_ports()
        await asyncio.sleep(60)


app = VersionedFastAPI(
    fast_api_app,
    version="1.0.0",
    prefix_format="/v{major}.{minor}",
    enable_latest=True,
)

app.mount("/", StaticFiles(directory=str(HTML_FOLDER), html=True))

if __name__ == "__main__":
    loop = asyncio.new_event_loop()

    # Running uvicorn with log disabled so loguru can handle it
    config = Config(app=app, loop=loop, host="0.0.0.0", port=Helper.PORT, log_config=None)
    server = Server(config)

    loop.create_task(periodic())
    loop.run_until_complete(server.serve())
