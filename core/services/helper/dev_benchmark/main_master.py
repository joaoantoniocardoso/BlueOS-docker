#!/usr/bin/env python3

import asyncio
import http
import logging
import os
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import machineid
import psutil
import requests
from bs4 import BeautifulSoup
from commonwealth.utils.apis import GenericErrorHandlingRoute, PrettyJSONResponse

# from commonwealth.utils.decorators import temporary_cache
from commonwealth.utils.logs import InterceptHandler, init_logger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi_versioning import VersionedFastAPI, version
from pydantic import BaseModel
from speedtest import Speedtest
from uvicorn import Config, Server

PORT = 9081
DOCS_CANDIDATE_URLS = ["/docs", "/v1.0/ui/"]
API_CANDIDATE_URLS = ["/docs.json", "/openapi.json", "/swagger.json"]

BLUEOS_VERSION = os.environ.get("GIT_DESCRIBE_TAGS", "null")
HTML_FOLDER = Path.joinpath(Path(__file__).parent.absolute(), "html")
SPEED_TEST: Optional[Speedtest] = None


logging.basicConfig(handlers=[InterceptHandler()], level=0)
init_logger("Helper")

try:
    SPEED_TEST = Speedtest(secure=True)
except Exception:
    # When starting, the system may not be connected to the internet
    pass


class Website(str, Enum):
    ArduPilot = "http://firmware.ardupilot.org"
    AWS = "http://amazon.com"
    BlueOS = f"http://blueos.cloud/ping?id={machineid.hashed_id()}&version={BLUEOS_VERSION}"
    Cloudflare = "http://1.1.1.1/"
    GitHub = "http://github.com"


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


class Helper:
    LOCALSERVER_CANDIDATES = ["0.0.0.0", "::"]
    AIOTIMEOUT = aiohttp.ClientTimeout(total=10)
    SKIP_PORTS = [
        PORT,  # Itself
        22,  # SSH
        80,  # BlueOS
        6021,  # Mavlink Camera Manager's WebRTC signaller
        8554,  # Mavlink Camera Manager's RTSP server
        5777,  # ardupilot-manager's Mavlink TCP Server
        5555,  # DGB server
        2770,  # NGINX
        9081,
        9082,
        9083,
    ]

    @staticmethod
    # @temporary_cache(timeout_seconds=60)  # a temporary cache helps us deal with changes in metadata
    def detect_service(port: int) -> ServiceInfo:
        info = ServiceInfo(valid=False, title="Unknown", documentation_url="", versions=[], port=port)

        try:
            with requests.get(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                info.valid = True
                soup = BeautifulSoup(response.text, features="html.parser")
                title_element = soup.find("title")
                info.title = title_element.text if title_element else "Unknown"
        except Exception:
            # The server is not available, any error code will be handle by the 'with' block
            pass

        # If not valid web server, documentation will not be available
        if not info.valid:
            return info

        # Check for service description metadata
        try:
            with requests.get(f"http://127.0.0.1:{port}/register_service", timeout=0.2) as response:
                if response.status_code == http.HTTPStatus.OK:
                    info.metadata = ServiceMetadata.parse_obj(response.json())
                    info.metadata.sanitized_name = re.sub(r"[^a-z0-9]", "", info.metadata.name.lower())
        except Exception:
            # This should be avoided by the first try block, but better safe than sorry
            pass

        for documentation_path in DOCS_CANDIDATE_URLS:
            try:
                with requests.get(f"http://127.0.0.1:{port}{documentation_path}", timeout=0.2) as response:
                    if response.status_code != http.HTTPStatus.OK:
                        continue
                    info.documentation_url = documentation_path

                # Get main openapi json description file
                for api_path in API_CANDIDATE_URLS:
                    with requests.get(f"http://127.0.0.1:{port}{api_path}", timeout=0.2) as response:
                        if response.status_code != http.HTTPStatus.OK:
                            continue
                        api = response.json()
                        # Check all available versions
                        ## The ones that provide a swagger-ui
                        for path in api["paths"].keys():
                            with requests.get(f"http://127.0.0.1:{port}{path}", timeout=0.2) as response:
                                if "swagger-ui" in response.text:
                                    info.versions += [path]
                break

            except Exception:
                # This should be avoided by the first try block, but better safe than sorry
                break
        return info

    @staticmethod
    # @temporary_cache(timeout_seconds=10)
    def scan_ports() -> List[ServiceInfo]:
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
        return [service for service in services if service.valid]

    @staticmethod
    async def check_website(site: Website) -> WebsiteStatus:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(site.value, timeout=Helper.AIOTIMEOUT) as response:
                    await response.text()
                    return WebsiteStatus(site=site, online=True)
            except Exception as exception:
                return WebsiteStatus(site=site, online=False, error=str(exception))

    @staticmethod
    async def check_internet_access() -> Dict[str, WebsiteStatus]:
        tasks = [Helper.check_website(site) for site in Website]
        status_list = await asyncio.gather(*tasks)
        return {status.site.name: status for status in status_list}


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
async def check_internet_access() -> Any:
    return await Helper.check_internet_access()


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
        await asyncio.sleep(60)
        await Helper.check_internet_access()


app = VersionedFastAPI(
    fast_api_app,
    version="1.0.0",
    prefix_format="/v{major}.{minor}",
    enable_latest=True,
)

# app.mount("/", StaticFiles(directory=str(HTML_FOLDER), html=True))

if __name__ == "__main__":
    loop = asyncio.new_event_loop()

    # Running uvicorn with log disabled so loguru can handle it
    config = Config(app=app, loop=loop, host="0.0.0.0", port=9081, log_config=None)
    server = Server(config)

    # loop.create_task(periodic())
    loop.run_until_complete(server.serve())
