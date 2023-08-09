import asyncio
import json
import logging
import logging.config

from dotenv import load_dotenv

from observer import SaleFinderSubject, FilteredDiscordObserver, FilteredTwitterObserver
import configs.constants as constants

load_dotenv()
with open(f"{constants.CONFIG_PATH}//log_config.json", "r", encoding="UTF-8") as stream:
    config = json.load(stream)
logging.config.dictConfig(config)
logger = logging.getLogger("standard")
with open(f"{constants.CONFIG_PATH}/config.json", encoding="UTF-8") as g:
    configs = json.load(g)


async def main():
    sale_finder_subject = SaleFinderSubject()

    for channel in configs["discordChannels"]:
        if configs["discordChannels"][channel]["turnedOn"]:
            observer = FilteredDiscordObserver(configs["discordChannels"][channel])
            if configs["discordChannels"][channel]["filter"]:
                observer.set_filter(configs["discordChannels"][channel]["filter"])
            sale_finder_subject.attach(observer)

    for twitter_account in configs["twitterBots"]:
        if configs["twitterBots"][twitter_account]["turnedOn"]:
            observer = FilteredTwitterObserver(configs["twitterBots"][twitter_account])
            if configs["twitterBots"][twitter_account]["filter"]:
                observer.set_filter(configs["twitterBots"][twitter_account]["filter"])
            sale_finder_subject.attach(observer)

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
