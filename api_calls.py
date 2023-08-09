import os
import time
import json
import logging
import logging.config
import asyncio
from typing import Optional

import httpx
from dotenv import load_dotenv

import configs.constants as constants

logger = logging.getLogger("standard")
with open(f"{constants.CONFIG_PATH}/config.json", encoding="UTF-8") as g:
    configs = json.load(g)
load_dotenv()
JOEPEGS_API_KEY: Optional[str] = os.getenv("JOEPEGS_API_KEY")


class ApiCalls:
    def __init__(self) -> None:
        self.connection_tries = 3
        header = {"x-joepegs-api-key": JOEPEGS_API_KEY}
        self.client = httpx.AsyncClient(headers=header)

    async def close_client(self) -> None:
        await self.client.aclose()
        logger.debug("Client closed")

    async def ask_joepegs_about_recent_sales(self) -> dict:
        recent_sales_amount = configs["general"]["recentSalesAmount"]
        if int(recent_sales_amount) > 100:
            raise Exception("recentSalesAmount cannot exceed 100. Modify config")
        url = f"https://api.joepegs.dev/v2/items?pageSize={recent_sales_amount}&pageNum=1&orderBy=recent_sale"
        response = await self.ask_jopegs_internal(url, "ask_joepegs_about_sale")
        return response

    async def ask_thegraph_avax_price(self) -> float:
        url = "https://api.thegraph.com/subgraphs/name/traderjoe-xyz/exchange"
        query = """
            {
            bundles(first: 5) {
                id
                avaxPrice
            }
            }
            """
        responseJSON = await self.ask_thegraph_internal(url, query)
        try:
            avax_price = responseJSON["data"]["bundles"][0]["avaxPrice"]
        except:
            avax_price = 0
            logger.warning("Returning AVAX price = 0")
        return avax_price

    async def ask_thegraph_internal(self, url: str, query: str) -> dict:
        for tries in range(self.connection_tries):
            try:
                request = await self.client.post(url, json={"query": query})
                if request.status_code == 200:
                    logger.debug("got an answer from ask_thegraph_internal")
                    return request.json()
                else:
                    raise Exception("status code != 200")
            except Exception as e:
                logger.debug("ask_thegraph_internal failed. %s tries left", 2 - tries)
                time.sleep(1 + 1 * tries)
                if tries == self.connection_tries - 1:
                    logger.warning("Error during request: %s", url, exc_info=True)

    async def ask_joepegs_about_floor(self, contract_id: str) -> float:
        url = f"https://api.joepegs.dev/v2/collections/{contract_id}"
        response = await self.ask_jopegs_internal(url, "ask_joepegs_about_floor")
        try:
            floor = round(float(response["floor"]) * 10 ** (-18), 2)
        except:
            logger.warning(
                "Returning floor price = 0 for %s", contract_id, exc_info=True
            )
            floor = 0
        return floor

    async def ask_joepegs_about_sale(
        self, contract_id: str, token_id: int
    ) -> list[dict]:
        url = f"https://api.joepegs.dev/v2/activities/{contract_id}/tokens/{token_id}?pageSize=2&pageNum=1&filters=sale"
        return await self.ask_jopegs_internal(url, "ask_joepegs_about_sale")

    async def ask_jopegs_internal(self, url: str, parent_function: str):
        for request_attempt in range(self.connection_tries):
            try:
                request = await self.client.get(url)
                if request.status_code == 200:
                    logger.debug("url ok: %s", url)
                    return request.json()
                elif request.status_code == 401:
                    logger.critical("Invalid or missing API key in .env file. Exiting.")
                    exit(1)
                elif request.status_code == 429:
                    logger.warning("Waiting. API rate limit exceeded during %s", url)
                    """Blocking code to wait until API rate limiting ends"""
                    await asyncio.sleep(15)
                    raise
                else:
                    raise
            except Exception:
                logger.debug(
                    "%s failed due to status code %s. %s request attempts left",
                    parent_function,
                    request.status_code,
                    2 - request_attempt,
                )
                if request_attempt == self.connection_tries - 1:
                    logger.warning("Error during request: %s", url)
                    logger.warning(
                        "Response status = %s, response = %s",
                        request.status_code,
                        request.json(),
                        exc_info=True,
                    )
