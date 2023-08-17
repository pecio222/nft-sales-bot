import asyncio
import json
import logging
import logging.config

from dotenv import load_dotenv

from observer import SaleFinderSubject, FilteredDiscordObserver, FilteredTwitterObserver
from api_calls import TwitterImageUploader
import configs.constants as constants

load_dotenv()
with open(f"{constants.CONFIG_PATH}/log_config.json", "r", encoding="UTF-8") as stream:
    config = json.load(stream)
logging.config.dictConfig(config)
logger = logging.getLogger("standard")
with open(f"{constants.CONFIG_PATH}/config.json", encoding="UTF-8") as g:
    configs = json.load(g)


async def main():
    sale_finder_subject = SaleFinderSubject()

    for _, channel_configs in configs["discordChannels"].items():
        if channel_configs["turnedOn"]:
            observer = FilteredDiscordObserver(channel_configs)

            collectionFilter = channel_configs["collectionFilter"]
            priceFilter = channel_configs["priceFilter"]

            if collectionFilter:
                observer.set_collection_filter(collectionFilter)
            if priceFilter:
                observer.set_min_price(priceFilter)

            sale_finder_subject.attach(observer)

    for _, bot_configs in configs["twitterBots"].items():
        if bot_configs["turnedOn"]:
            observer = FilteredTwitterObserver(bot_configs)
            collectionFilter = bot_configs["collectionFilter"]

            priceFilter = bot_configs["priceFilter"]

            if collectionFilter:
                observer.set_collection_filter(collectionFilter)
            if priceFilter:
                observer.set_min_price(priceFilter)

            sale_finder_subject.attach(observer)

            # TODO overwrites when more twitter clients
            twitter_uploader = TwitterImageUploader(bot_configs)
            sale_finder_subject.set_twitter_uploader(twitter_uploader)
    while True:
        try:
            await sale_finder_subject.run()
        except Exception as e:
            logger.critical(
                "Something failed horribly. Will try to run again soon", exc_info=True
            )
        finally:
            await sale_finder_subject.api_calls.close_client()

        logger.info(
            "Job is done. Waiting %s seconds.",
            configs["general"]["salesCallIntervalSeconds"],
        )
        await asyncio.sleep(configs["general"]["salesCallIntervalSeconds"])


if __name__ == "__main__":
    asyncio.run(main())
