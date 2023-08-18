from __future__ import annotations
import operator
import os
import io
import json
import time
import logging
import logging.config
from typing import Optional
from abc import ABC, abstractmethod
import asyncio

from discord_webhook import DiscordEmbed, DiscordWebhook
import tweepy

from data_classes import EmbedData, ItemSale
from api_calls import ApiCalls, TwitterImageUploader
import configs.constants as constants

with open(f"{constants.CONFIG_PATH}/log_config.json", "r", encoding="UTF-8") as stream:
    config = json.load(stream)
logging.config.dictConfig(config)
logger = logging.getLogger("standard")
with open(f"{constants.CONFIG_PATH}/config.json", encoding="UTF-8") as g:
    configs = json.load(g)


class SaleFinderSubject:
    """
    Manages observers and filter. Gets sales data, processes it and notifies observers.
    """

    token_sale_list: list[ItemSale]
    raw_sales_data: dict = {}
    avax_price_in_usd: float = 0
    sale_to_notify: ItemSale
    embed_data: EmbedData
    _observers: list[FilteredObserver] = []
    _observed_collections: list[str] = []
    _last_notified_transactions: list[str]
    last_checked_avax_price_at: float = 0

    def __init__(self) -> None:
        with open(
            f"{constants.CONFIG_PATH}/last_notified_transactions.json", encoding="UTF-8"
        ) as file:
            self._last_notified_transactions = json.load(file)
        self.JOEPEGS_API_KEY: Optional[str] = os.getenv("JOEPEGS_API_KEY")

    def attach(self, observer: FilteredObserver) -> None:
        self._observers.append(observer)
        logger.debug("Subject: Attached an observer.")

    def detach(self, observer: FilteredObserver) -> None:
        self._observers.remove(observer)
        logger.debug("Subject: Detached an observer.")

    def set_filter(self, collection_ids: list[str]) -> None:
        self._observed_collections = [
            collection.lower() for collection in collection_ids
        ]
        logger.warning(
            "Filter was set up. Will only notify about these collections: %s",
            self._observed_collections,
        )

    def set_twitter_uploader(self, twitter_uploader: TwitterImageUploader) -> None:
        self.twitter_uploader = twitter_uploader

    async def run(self) -> None:
        """
        Get sales data, process it, notify observers and save notified.
        """
        logger.debug("Run method executing")
        self.api_calls = ApiCalls()
        self.raw_sales_data: list[
            dict
        ] = await self.api_calls.ask_joepegs_about_recent_sales()

        if self._new_sales():
            logger.debug("New sales were found")
            await self.get_avax_price_in_usd()
            self._choose_sales_to_get_data_about()
            await self._get_sales_data_from_joepegs()
            self._filter_and_notify()
            self._write_notified_sales_to_file()
        else:
            logger.debug("New sales were not found")

    def _new_sales(self):
        """
        Check if observers were notified about latest sale
        """
        return self.raw_sales_data[0]["id"] not in self._last_notified_transactions

    async def get_avax_price_in_usd(self):
        last_checked_avax_price_ago = time.time() - self.last_checked_avax_price_at
        if last_checked_avax_price_ago > constants.GET_AVAX_PRICE_FREQUENCY:
            avax_price_in_usd = await self.api_calls.ask_thegraph_avax_price()
            if avax_price_in_usd:
                self.avax_price_in_usd = avax_price_in_usd
                self.last_checked_avax_price_at = time.time()
                logger.debug(
                    "Avax price = %s at %s",
                    self.avax_price_in_usd,
                    self.last_checked_avax_price_at,
                )

    def _choose_sales_to_get_data_about(self):
        self.sales_to_get_data_about = [
            raw_sale
            for raw_sale in self.raw_sales_data
            if raw_sale["id"] not in self._last_notified_transactions
            and raw_sale["verified"] != "blocklisted"
        ]

    async def _get_sales_data_from_joepegs(self):
        self.token_sale_list = []
        sales_amount = len(self.sales_to_get_data_about)
        max_chunk_size = 15
        if sales_amount > max_chunk_size:
            chunks = [
                self.sales_to_get_data_about[x : x + max_chunk_size]
                for x in range(0, sales_amount, max_chunk_size)
            ]
            logger.debug(
                "Retrieving more than %s sales at the same time - splitting API calls into %s chunks",
                max_chunk_size,
                len(chunks),
            )
            for chunk in chunks:
                smaller_task_list = [
                    self._get_single_sale_data_from(raw_sale) for raw_sale in chunk
                ]
                await asyncio.gather(*smaller_task_list)
                logger.debug("Waiting to avoid rate limiting")
                await asyncio.sleep(5)
        else:
            full_task_list = [
                self._get_single_sale_data_from(raw_sale)
                for raw_sale in self.sales_to_get_data_about
            ]
            await asyncio.gather(*full_task_list)

    async def _get_single_sale_data_from(self, raw_sale):
        task_list = [
            self.api_calls.ask_joepegs_about_sale(
                raw_sale["collection"],
                raw_sale["tokenId"],
            ),
            self.api_calls.ask_joepegs_about_floor(raw_sale["collection"]),
        ]
        try:
            async_result = await asyncio.gather(*task_list)
        except Exception:
            logger.warn(
                "_get_single_sale_data_from failed for %s",
                raw_sale["id"],
                exc_info=True,
            )
            return
        _api_last_sale = async_result[0]
        _price_floor = async_result[1]

        if not _api_last_sale:
            logger.warning(
                "ask_joepegs_about_sale returned None. Can't notify about this sale, will try again later %s/%s",
                raw_sale["collection"],
                raw_sale["tokenId"],
            )
            return
        else:
            time_from_sale = round(time.time() - _api_last_sale[0]["timestamp"])
            if time_from_sale > configs["general"]["oldestSaleToNotify"]:
                """
                There is sometimes delay while asking about last sale on the indexer side.
                This will ignore result, if ask_joepegs_about_sale returned sale that are older than oldestSaleToNotify.
                Protects both against notifying about old sales and sending wrong "last sold for" field
                """
                logger.debug(
                    "ask_joepegs_about_sale returned sale that is %s seconds old. Can't notify about this sale, will try again later %s/%s",
                    time_from_sale,
                    raw_sale["collection"],
                    raw_sale["tokenId"],
                )
                return

        self.token_sale_list.append(
            ItemSale(
                avax_price_in_usd=self.avax_price_in_usd,
                raw_sale=raw_sale,
                price_floor=_price_floor,
                last_sales=_api_last_sale,
            )
        )

    def _filter_and_notify(self):
        """Sorts by timestamp, so sales are sent oldest first"""
        self.sorted_token_sale_list = sorted(
            self.token_sale_list, key=operator.attrgetter("sort_index")
        )

        for sale in self.sorted_token_sale_list:
            if self.filter_collections(sale.contract_id):
                self.sale_to_notify: ItemSale = sale  # todo
                self.embed_data = EmbedData(sale)
                self.discord_embed = self._prepare_discord_embed()
                self.twitter_media_id = self.twitter_uploader.get_media_id(
                    sale.img_link, sale.svg_bytes
                )
                self._notify()
                self._last_notified_transactions.append(sale.transaction_id)

    def filter_collections(self, contract_id: str) -> bool:
        return (
            contract_id in self._observed_collections
            or len(self._observed_collections) == 0
        )

    def _prepare_discord_embed(self):
        embed = DiscordEmbed()
        embed_data = self.embed_data

        if embed_data.image_url:
            embed.set_image(url=embed_data.image_url)

        embed.add_embed_field(
            name=embed_data.embed_title_name,
            value=embed_data.embed_title_value,
            inline=False,
        )
        embed.set_thumbnail(url=embed_data.embed_thumbnail_url)

        embed.add_embed_field(
            name="Sold for",
            value=embed_data.sold_for_value,
            inline=True,
        )
        embed.add_embed_field(name="Last sold for", value=embed_data.last_sold_for)

        if embed_data.floor_text:
            embed.add_embed_field(
                name="Price Floor",
                value=embed_data.floor_value,
                inline=False,
            )

        embed.set_footer(text=embed_data.footer)
        return embed

    def _notify(self) -> None:
        """
        Trigger an update in each subscriber.
        """
        logger.info(
            "Notifying observers about transaction - %s was sold",
            self.sale_to_notify.item_name,
        )

        for observer in self._observers:
            observer.update(self)

    def _write_notified_sales_to_file(self):
        with open(
            f"{constants.CONFIG_PATH}/last_notified_transactions.json",
            mode="w",
            encoding="UTF-8",
        ) as file:
            _saving_amount = int(configs["general"]["recentSalesAmount"]) * 2
            if len(self._last_notified_transactions) > _saving_amount:
                self._last_notified_transactions = self._last_notified_transactions[
                    -_saving_amount:
                ]
            json.dump(self._last_notified_transactions, file)
            logger.debug(
                "_last_notified_transactions has %s elements and = %s",
                len(self._last_notified_transactions),
                self._last_notified_transactions,
            )


class FilteredObserver(ABC):
    def __init__(self) -> None:
        super().__init__()
        self._observed_collections: list = []
        self.min_price: float = 0

    def set_collection_filter(self, collection_ids: list[str]) -> None:
        self._observed_collections = [
            collection.lower() for collection in collection_ids
        ]
        logger.info(
            "Filter was set up for %s. Will only notify about these collections: %s",
            self.credentials["envName"],
            self._observed_collections,
        )

    def filter_by_collection(self, contract_id: str) -> bool:
        return (
            contract_id in self._observed_collections
            or self._observed_collections == []
        )

    def set_min_price(self, min_price: float) -> None:
        self.min_price = min_price
        logger.info(
            "Filter was set up for %s. Will only notify about sales over %s AVAX",
            self.credentials["envName"],
            self.min_price,
        )

    def filer_by_price(self, price: float) -> bool:
        return not self.min_price or price >= self.min_price

    def should_be_notified(self, contract_id: str, price: int) -> bool:
        return self.filter_by_collection(contract_id) and self.filer_by_price(price)

    @abstractmethod
    def update(self, data):
        pass


class FilteredDiscordObserver(FilteredObserver):
    """
    Sends Discord message when new sale happens
    """

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__()
        self.credentials: dict[str, str] = _credentials
        logger.info("Getting discord webhook url from %s", self.credentials["envName"])
        self.DISCORD_WEBHOOK_URL: Optional[str] = os.getenv(self.credentials["envName"])

    def update(self, subject: SaleFinderSubject) -> None:
        self.webhook = DiscordWebhook(
            url=self.DISCORD_WEBHOOK_URL,
            rate_limit_retry=True,
            content="",
            username=self.credentials["discordBotName"],
        )

        if not self.should_be_notified(
            subject.sale_to_notify.contract_id, subject.sale_to_notify.price_avax
        ):
            return

        self.webhook.add_embed(subject.discord_embed)
        if subject.sale_to_notify.svg_bytes:
            self.webhook.add_file(
                subject.sale_to_notify.svg_bytes, filename="image.png"
            )
        response = self.webhook.execute()
        if response.status_code == 200:
            logger.debug(
                "%s successfully sent %s sale notification to discord",
                self.credentials["envName"],
                subject.sale_to_notify.item_name,
            )
        else:
            logger.warning(
                "%s failed sending %s sale notification to discord due to status code %s, response %s",
                self.credentials["envName"],
                subject.sale_to_notify.item_name,
                response.status_code,
                response,
            )


class FilteredTwitterObserver(FilteredObserver):
    """
    Sends Tweet when new sale happens
    """

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__()
        self.credentials: dict[str, str] = _credentials
        logger.info("Getting twitter credentials from %s", self.credentials["envName"])

        env_name = self.credentials["envName"]
        access_token = os.environ.get(f"{env_name}_ACCESS_TOKEN")
        access_token_secret = os.environ.get(f"{env_name}_ACCESS_TOKEN_SECRET")
        consumer_key = os.environ.get(f"{env_name}_API_KEY")
        consumer_secret = os.environ.get(f"{env_name}_API_KEY_SECRET")

        self.client = tweepy.Client(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )

    def update(self, subject: SaleFinderSubject) -> None:
        if not self.should_be_notified(
            subject.sale_to_notify.contract_id, subject.sale_to_notify.price_avax
        ):
            return None

        if subject.embed_data.last_sold_for == constants.NEVER_SOLD:
            last_sold = constants.NEVER_SOLD
        else:
            last_sold = f"Last sold for {subject.embed_data.last_sold_for}"

        tweet_text = (
            subject.embed_data.token_sale.item_name
            + " sold for "
            + subject.embed_data.sold_for_value
            + "\n"
            + last_sold
            + "\n"
            + subject.embed_data.joepegs_token_url
        )

        try:
            if subject.twitter_media_id:
                self.client.create_tweet(
                    text=tweet_text, media_ids=[subject.twitter_media_id]
                )
            else:
                self.client.create_tweet(text=tweet_text)
        except Exception as e:
            logger.warning(
                "Failed to send tweet about %s. Exception: %s",
                subject.sale_to_notify.item_name,
                e,
            )
