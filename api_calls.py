import os
import io
import time
import json
import logging
import logging.config
import asyncio
import urllib
from typing import Optional, Any

import httpx
import tweepy
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
        url = f"https://api.joepegs.dev/v3/items?pageSize={recent_sales_amount}&chains=['avalanche']&pageNum=1&orderBy=recent_sale"
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
        url = f"https://api.joepegs.dev/v3/collections/avalanche/{contract_id}"
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
        url = f"https://api.joepegs.dev/v3/activities/avalanche/{contract_id}/tokens/{token_id}?pageSize=2&pageNum=1&filters=sale"
        return await self.ask_jopegs_internal(url, "ask_joepegs_about_sale")

    async def ask_jopegs_internal(self, url: str, parent_function: str):
        for request_attempt in range(self.connection_tries):
            # dummy response to avoid UnboundLocalError: local variable 'request' referenced before assignment
            request = httpx.Response(666)
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
                    if request.status_code == 666:
                        logger.warning(
                            "Error during request: %s request variable wasn't referenced",
                            url,
                        )
                    else:
                        logger.warning("Error during request: %s", url)
                        logger.warning(
                            "Response status = %s, response = %s",
                            request.status_code,
                            request.json(),
                            exc_info=True,
                        )


class TwitterImageUploader:
    def __init__(self, credentials) -> None:
        # connect to twitter API
        env_name = credentials["envName"]
        access_token = os.environ.get(f"{env_name}_ACCESS_TOKEN")
        access_token_secret = os.environ.get(f"{env_name}_ACCESS_TOKEN_SECRET")
        consumer_key = os.environ.get(f"{env_name}_API_KEY")
        consumer_secret = os.environ.get(f"{env_name}_API_KEY_SECRET")

        auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
        auth.set_access_token(access_token, access_token_secret)
        self.api = tweepy.API(auth)

    def get_media_id(self, url: str, bytes) -> int:
        image = self.get_image_from_url(url, bytes)
        media_id = self.upload_image_to_twitter(image)
        return media_id

    def get_image_from_url(self, url: str, bytes) -> io.BytesIO:
        if bytes:
            # handle SVG upload
            return io.BytesIO(bytes)

        url = urllib.parse.unquote(url)
        try:
            response = httpx.get(url)
        except Exception:
            logger.warning("Failed to download image from %s", url, exc_info=True)
            return ""
        if response.status_code == 200:
            content_length = int(response.headers.get("Content-Length", 0))
            if content_length <= 5 * 1024 * 1024:  # 5MB in bytes
                file_io = io.BytesIO(response.content)
                logger.debug("Image from %s downloaded", url)
                return file_io
            else:
                logger.warning(
                    "Image from %s is too big. Content-Length: %s",
                    url,
                    content_length,
                )
                return ""

        else:
            logger.warning(
                "Failed to download image from %s. Status code: %s",
                url,
                response.status_code,
            )
            return ""

    def upload_image_to_twitter(self, file_io: Any) -> int:
        if file_io:
            try:
                media = self.api.media_upload(filename="upload", file=file_io)
                return media.media_id
            except Exception as e:
                logger.warning(
                    "Failed to upload image to twitter due to %s", e, exc_info=True
                )
                return 0

        else:
            return 0
