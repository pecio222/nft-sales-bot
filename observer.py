from __future__ import annotations
import operator
import os
import json
import time
import logging
import logging.config
from typing import Optional
import asyncio

from discord_webhook import DiscordEmbed, DiscordWebhook

from data_classes import EmbedData, TokenSale
from api_calls import ApiCalls


with open("configs/log_config.json", "r", encoding="UTF-8") as stream:
    config = json.load(stream)
logging.config.dictConfig(config)
logger = logging.getLogger("standard")
with open("configs/config.json", encoding="UTF-8") as g:
    configs = json.load(g)


class SaleFinderSubject:
    """
    Manages observers and filter. Gets sales data, processes it and notifies observers.
    """

    token_sale_list: list[TokenSale]
    raw_sales_data: dict = {}
    avax_price_in_usd: float = 0
    sale_to_notify: TokenSale
    embed_data: EmbedData
    _observers: list[FilteredDiscordObserver] = []
    _observed_collections: list[str] = []
    _last_notified_transactions: list[str]

    def __init__(self) -> None:
        with open("configs/lastNotifiedTransactions.json", encoding="UTF-8") as file:
            self._last_notified_transactions = json.load(file)
        self.JOEPEGS_API_KEY: Optional[str] = os.getenv("JOEPEGS_API_KEY")

    def attach(self, observer: FilteredDiscordObserver) -> None:
        self._observers.append(observer)
        logger.debug("Subject: Attached an observer.")

    def detach(self, observer: FilteredDiscordObserver) -> None:
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

    async def run(self) -> None:
        """
        Get sales data, process it, notify observers and save notified.
        """
        logger.debug("Run method executing")
        self.api_calls = ApiCalls()
        self.raw_sales_data: list[
            dict
        ] = await self.api_calls.ask_joepegs_about_recent_sales()

        if self.new_sales():
            logger.debug("New sales were found")
            avax_price_in_usd = await self.api_calls.ask_thegraph_avax_price()
            if avax_price_in_usd:
                self.avax_price_in_usd = avax_price_in_usd
            self.remove_notified_sales()
            await self.get_sales_data_from_joepegs()
            self.filter_and_notify()
            self.write_notified_sales_to_file()
        else:
            logger.debug("New sales were not found")

    def new_sales(self):
        """
        Check if observers were notified about latest sale
        """
        return self.raw_sales_data[0]["id"] not in self._last_notified_transactions

    def remove_notified_sales(self):
        self.sales_to_notify = [
            raw_sale
            for raw_sale in self.raw_sales_data
            if raw_sale["id"] not in self._last_notified_transactions
        ]

    async def get_sales_data_from_joepegs(self):
        self.token_sale_list = []
        sales_amount = len(self.sales_to_notify)
        max_chunk_size = 15
        if sales_amount > max_chunk_size:
            chunks = [
                self.sales_to_notify[x : x + max_chunk_size]
                for x in range(0, len(self.sales_to_notify), max_chunk_size)
            ]
            logger.debug(
                "Retrieving more than %s sales at the same time - splitting API calls into %s chunks",
                max_chunk_size,
                len(chunks),
            )
            for chunk in chunks:
                smaller_task_list = []
                for raw_sale in chunk:
                    smaller_task_list.append(self.get_single_sale_data_from(raw_sale))
                await asyncio.gather(*smaller_task_list)
                logger.debug("Waiting to avoid rate limiting")
                await asyncio.sleep(5)
        else:
            full_task_list = []
            for raw_sale in self.sales_to_notify:
                full_task_list.append(self.get_single_sale_data_from(raw_sale))
            await asyncio.gather(*full_task_list)

    async def get_single_sale_data_from(self, raw_sale):
        task_list = [
            self.api_calls.ask_joepegs_about_sale(
                raw_sale["collection"],
                raw_sale["tokenId"],
            ),
            self.api_calls.ask_joepegs_about_floor(raw_sale["collection"]),
        ]
        start = time.perf_counter()
        try:
            async_result = await asyncio.gather(*task_list)
        except Exception:
            logger.warn(
                "get_single_sale_data_from failed for %s",
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
            TokenSale(
                avax_price_in_usd=self.avax_price_in_usd,
                raw_sale=raw_sale,
                price_floor=_price_floor,
                last_sales=_api_last_sale,
            )
        )

    def filter_and_notify(self):
        """Sorts by timestamp, so sales are sent oldest first"""
        self.sorted_token_sale_list = sorted(
            self.token_sale_list, key=operator.attrgetter("sort_index")
        )

        for sale in self.sorted_token_sale_list:
            if self.filter_collections(sale.contractId):
                self.sale_to_notify: TokenSale = sale  # todo
                self.embed = self.prepare_embed(sale)
                self.notify()
                self._last_notified_transactions.append(sale.transactionID)

    def filter_collections(self, contractID: str) -> bool:
        return (
            contractID in self._observed_collections or self._observed_collections == []
        )

    def prepare_embed(self, sale):
        embed = DiscordEmbed()
        embed_data = EmbedData(sale)
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

    def notify(self) -> None:
        """
        Trigger an update in each subscriber.
        """
        logger.info(
            "Notifying observers about transaction - %s was sold",
            self.sale_to_notify.NFTname,
        )

        for observer in self._observers:
            observer.update(self)

    def write_notified_sales_to_file(self):
        with open(
            "configs/lastNotifiedTransactions.json", mode="w", encoding="UTF-8"
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

    def already_notified(self, transactionID: str) -> bool:
        return transactionID.lower() in self._last_notified_transactions


class FilteredDiscordObserver:
    """
    Sends Discord message when new sale happens
    """

    def __init__(self, _credentials: dict[str, str]) -> None:
        self.credentials: dict[str, str] = _credentials
        logger.info(
            "Getting discord webhook url from %s", self.credentials["envWebhookName"]
        )
        self.DISCORD_WEBHOOK_URL: Optional[str] = os.getenv(
            self.credentials["envWebhookName"]
        )
        self._observed_collections: list = []

    def set_filter(self, collection_ids: list[str]) -> None:
        self._observed_collections = [
            collection.lower() for collection in collection_ids
        ]
        logger.info(
            "Filter was set up for %s. Will only notify about these collections: %s",
            self.credentials["envWebhookName"],
            self._observed_collections,
        )

    def filter_collections(self, contract_id: str) -> bool:
        return (
            contract_id in self._observed_collections
            or self._observed_collections == []
        )

    def update(self, subject: SaleFinderSubject) -> None:
        self.webhook = DiscordWebhook(
            url=self.DISCORD_WEBHOOK_URL,
            rate_limit_retry=True,
            content="",
            username=self.credentials["discordBotName"],
        )

        if self.filter_collections(subject.sale_to_notify.contractId):
            self.webhook.add_embed(subject.embed)
            response = self.webhook.execute()
            if response.status_code == 200:
                logger.debug(
                    "%s successfully sent %s sale notification to discord",
                    self.credentials["envWebhookName"],
                    subject.sale_to_notify.NFTname,
                )
            else:
                logger.warning(
                    "%s failed sending %s sale notification to discord due to status code %s, response %s",
                    self.credentials["envWebhookName"],
                    subject.sale_to_notify.NFTname,
                    response.status_code,
                    response,
                )