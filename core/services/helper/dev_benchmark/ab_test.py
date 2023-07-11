import time
import requests
import json
import psutil
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

HOSTNAME = "blueos.local"
NUMBER_OF_TESTS = 100


@dataclass
class RequestData:
    response: str
    duration: float


@dataclass
class RequestTest:
    web_services: List[RequestData]
    check_internet_access: List[RequestData]

    class RequestDataEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, RequestData):
                return obj.__dict__
            return super().default(obj)

    def save_to_file(self, name: str, port: int) -> None:
        with open(f"request_results_{name}_port_{port}.json", "w") as file:
            json.dump(self.__dict__, file, indent=4, cls=RequestTest.RequestDataEncoder)

        print(f"Request results for {name}, port {port} saved.")


def run_requests(url: str) -> List[RequestData]:
    requests_data: List[RequestData] = []

    for i in range(NUMBER_OF_TESTS):
        response = requests.get(url)

        requests_data.append(
            RequestData(
                duration=response.elapsed.total_seconds(),
                response=response.json(),
            )
        )

    return requests_data


def run_request(hostname: str, name: str, port: int) -> RequestTest:
    web_services = run_requests(f"http://{hostname}:{port}/latest/web_services")
    check_internet_access = run_requests(f"http://{hostname}:{port}/latest/check_internet_access")

    return RequestTest(
        web_services=web_services,
        check_internet_access=check_internet_access,
    )


if __name__ == "__main__":
    implementations = [
        # {"name": "main_master", "port": 81},
        # {"name": "main_master_with_skip_list", "port": 9081},
        # {"name": "main_opt", "port": 9082},
        # {"name": "main_opt2", "port": 9083},
        # {"name": "main_httpx", "port": 9084},
        # {"name": "main_opt3", "port": 9085},
        # {"name": "main_opt4", "port": 9086},
        # {"name": "main_opt4_cached", "port": 9087},
    ]

    for implementation in implementations:
        name = implementation["name"]
        port = implementation["port"]

        request_results = run_request(HOSTNAME, name, port)

        print(f"Request for {name}, port {port} completed")

        request_results.save_to_file(name, port)
