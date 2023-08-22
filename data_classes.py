from dataclasses import dataclass, field
from datetime import datetime
import configs.constants as constants
import json
import logging
import logging.config
import urllib.parse

import base64
from cairosvg import svg2png

with open(f"{constants.CONFIG_PATH}/log_config.json", "r", encoding="UTF-8") as stream:
    config = json.load(stream)
logging.config.dictConfig(config)
logger = logging.getLogger("standard")


@dataclass
class ItemSale:
    """
    Data of given sale transaction
    """

    avax_price_in_usd: float
    raw_sale: dict
    price_floor: float
    last_sales: list

    def __post_init__(self):
        """
        self.last_sales is protected against being empty
        ItemSale will not be created for items that didn't receive data
        """
        self.sort_index = int(self.last_sales[0]["timestamp"])
        self.transaction_id: str = self.raw_sale["id"]
        self.transaction_hash: str = self.last_sales[0]["transactionHash"]
        self.transaction_link: str = f"https://snowtrace.io/tx/{self.transaction_hash}"
        self.contract_id: str = self.raw_sale["collection"]
        self.token_id: int = self.raw_sale["tokenId"]
        self.verified: str = self.raw_sale["verified"]
        self.date: datetime = datetime.utcfromtimestamp(
            int(self.last_sales[0]["timestamp"])
        )
        self.price_avax = round(int(self.last_sales[0]["price"]) * 10 ** (-18), 3)
        self.price_usd: float = round(
            float(self.avax_price_in_usd) * self.price_avax, 1
        )

        self.is_taker_ask: bool = self.last_sales[0]["isTakerAsk"]

        self.sold_by_full = self.last_sales[0]["fromAddress"]
        self.bought_by_full = self.last_sales[0]["toAddress"]
        self.sold_by: str = self.sold_by_full[:5] + "..." + self.sold_by_full[-4:]
        self.bought_by: str = self.bought_by_full[:5] + "..." + self.bought_by_full[-4:]

        if self.last_sales[0]["image"]:
            # TODO create image from svg
            if "data:image/svg+xml;base64," in self.last_sales[0]["image"]:
                self.img_link = ""
                encoded = self.last_sales[0]["image"]
                encoded = encoded.replace("data:image/svg+xml;base64,", "")
                decoded = base64.b64decode(encoded)
                self.svg_bytes = svg2png(bytestring=decoded)

            else:
                self.img_link: str = urllib.parse.quote(self.last_sales[0]["image"])
                self.svg_bytes = ""
        else:
            self.img_link = ""
        self.collectionName: str = self.last_sales[0]["collectionName"]
        if len(self.last_sales) == 2:
            self.last_sale: float = round(
                int(self.last_sales[1]["price"]) * 10 ** (-18), 2
            )
        else:
            self.last_sale: float = None
        if self.last_sales[0]["name"] is not None:
            self.item_name: str = self.last_sales[0]["name"]
        elif self.last_sales[0]["collectionSymbol"] is not None:
            self.item_name: str = (
                str(self.last_sales[0]["collectionSymbol"]) + " #" + str(self.token_id)
            )
        else:
            self.item_name: str = str(self.collectionName) + " #" + str(self.token_id)


@dataclass
class EmbedData:
    """
    Set values that will be used to create embed sent to Discord
    """

    token_sale: ItemSale

    def __post_init__(self):
        self.image_url = f"{constants.JOEPEGS_CDN}{self.token_sale.img_link}"
        self.joepegs_token_url = f"https://joepegs.com/item/avalanche/{self.token_sale.contract_id}/{self.token_sale.token_id}"

        seller_url = f"https://joepegs.com/profile/{self.token_sale.sold_by_full}"
        buyer_url = f"https://joepegs.com/profile/{self.token_sale.bought_by_full}"

        if self.token_sale.is_taker_ask:
            self.taker_value = (
                f"[{self.token_sale.sold_by}](<{seller_url}>) "
                f"accepted bid by [{self.token_sale.bought_by}](<{buyer_url}>)"
            )
        else:
            self.taker_value = (
                f"[{self.token_sale.sold_by}](<{seller_url}>)'s "
                f"ask filled by [{self.token_sale.bought_by}](<{buyer_url}>)"
            )

        self.embed_title_value = f"New sale on JOEPEGS - [{self.token_sale.item_name}](<{self.joepegs_token_url}>) sold!"

        if self.token_sale.verified in ["verified", "verified_trusted"]:
            self.embed_title_name = f"{self.token_sale.collectionName}"
            self.embed_thumbnail_url = constants.THUMBNAIL_URLS["verified"]
        elif self.token_sale.verified == "unverified":
            self.embed_title_name = (
                f"{self.token_sale.collectionName} - {self.token_sale.verified}"
            )
            self.embed_thumbnail_url = constants.THUMBNAIL_URLS["unverified"]
        else:
            self.embed_title_name = ""
            self.embed_thumbnail_url = ""
            logger.warning(
                "verified parameter not in [verified, verified_trusted, unverified]. Has value = %s instead",
                self.token_sale.verified,
            )

        self.sold_for_value = (
            f"{self.token_sale.price_avax} AVAX [{self.token_sale.price_usd} USD]"
        )

        if self.token_sale.last_sale:
            self.last_sold_for = f"{self.token_sale.last_sale} AVAX"
        else:
            self.last_sold_for = "Never sold before!"

        self.floor_text = self.get_relation_with_floor()
        self.floor_value = f"{self.token_sale.price_floor} AVAX - {self.floor_text}\n{self.taker_value}"

        day = self.token_sale.date.strftime("%A")
        self.footer = f"Sold on {day[:3]}, {self.token_sale.date} UTC"

    def get_relation_with_floor(self) -> str:
        if self.token_sale.price_floor == 0:
            return ""
        elif self.token_sale.price_floor == self.token_sale.price_avax:
            return "Sold for floor price!"
        elif float(self.token_sale.price_floor) > self.token_sale.price_avax:
            return f"Sold {round(100 - 100 * self.token_sale.price_avax / self.token_sale.price_floor)}% below floor!"
        elif self.token_sale.price_floor < self.token_sale.price_avax:
            return f"Sold {round(100 * self.token_sale.price_avax / self.token_sale.price_floor - 100)}% over floor!"
        else:
            return ""
