import logging
import random
import re
import string
from collections.abc import MutableMapping
from typing import Any
from typing import cast

from retry import retry
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.models.blocks import Block
from slack_sdk.models.metadata import Metadata

from danswer.configs.constants import ID_SEPARATOR
from danswer.configs.constants import MessageType
from danswer.configs.danswerbot_configs import DANSWER_BOT_NUM_RETRIES
from danswer.connectors.slack.utils import make_slack_api_rate_limited
from danswer.connectors.slack.utils import SlackTextCleaner
from danswer.danswerbot.slack.constants import SLACK_CHANNEL_ID
from danswer.danswerbot.slack.tokens import fetch_tokens
from danswer.one_shot_answer.models import ThreadMessage
from danswer.utils.logger import setup_logger
from danswer.utils.text_processing import replace_whitespaces_w_space

logger = setup_logger()


DANSWER_BOT_APP_ID: str | None = None


def get_danswer_bot_app_id(web_client: WebClient) -> Any:
    global DANSWER_BOT_APP_ID
    if DANSWER_BOT_APP_ID is None:
        DANSWER_BOT_APP_ID = web_client.auth_test().get("user_id")
    return DANSWER_BOT_APP_ID


def remove_danswer_bot_tag(message_str: str, client: WebClient) -> str:
    bot_tag_id = get_danswer_bot_app_id(web_client=client)
    return re.sub(rf"<@{bot_tag_id}>\s", "", message_str)


class ChannelIdAdapter(logging.LoggerAdapter):
    """This is used to add the channel ID to all log messages
    emitted in this file"""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        channel_id = self.extra.get(SLACK_CHANNEL_ID) if self.extra else None
        if channel_id:
            return f"[Channel ID: {channel_id}] {msg}", kwargs
        else:
            return msg, kwargs


def get_web_client() -> WebClient:
    slack_tokens = fetch_tokens()
    return WebClient(token=slack_tokens.bot_token)


@retry(
    tries=DANSWER_BOT_NUM_RETRIES,
    delay=0.25,
    backoff=2,
    logger=cast(logging.Logger, logger),
)
def respond_in_thread(
    client: WebClient,
    channel: str,
    thread_ts: str | None,
    text: str | None = None,
    blocks: list[Block] | None = None,
    receiver_ids: list[str] | None = None,
    metadata: Metadata | None = None,
    unfurl: bool = True,
) -> None:
    if not text and not blocks:
        raise ValueError("One of `text` or `blocks` must be provided")

    if not receiver_ids:
        slack_call = make_slack_api_rate_limited(client.chat_postMessage)
    else:
        slack_call = make_slack_api_rate_limited(client.chat_postEphemeral)

    if not receiver_ids:
        response = slack_call(
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            metadata=metadata,
            unfurl_links=unfurl,
            unfurl_media=unfurl,
        )
        if not response.get("ok"):
            raise RuntimeError(f"Failed to post message: {response}")
    else:
        for receiver in receiver_ids:
            response = slack_call(
                channel=channel,
                user=receiver,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
                metadata=metadata,
                unfurl_links=unfurl,
                unfurl_media=unfurl,
            )
            if not response.get("ok"):
                raise RuntimeError(f"Failed to post message: {response}")


def build_feedback_block_id(
    message_id: int,
    document_id: str | None = None,
    document_rank: int | None = None,
) -> str:
    unique_prefix = "".join(random.choice(string.ascii_letters) for _ in range(10))
    if document_id is not None:
        if not document_id or document_rank is None:
            raise ValueError("Invalid document, missing information")
        if ID_SEPARATOR in document_id:
            raise ValueError(
                "Separator pattern should not already exist in document id"
            )
        block_id = ID_SEPARATOR.join([str(message_id), document_id, str(document_rank)])
    else:
        block_id = str(message_id)

    return unique_prefix + ID_SEPARATOR + block_id


def decompose_block_id(block_id: str) -> tuple[int, str | None, int | None]:
    """Decompose into query_id, document_id, document_rank, see above function"""
    try:
        components = block_id.split(ID_SEPARATOR)
        if len(components) != 2 and len(components) != 4:
            raise ValueError("Block ID does not contain right number of elements")

        if len(components) == 2:
            return int(components[-1]), None, None

        return int(components[1]), components[2], int(components[3])

    except Exception as e:
        logger.error(e)
        raise ValueError("Received invalid Feedback Block Identifier")


def translate_vespa_highlight_to_slack(match_strs: list[str], used_chars: int) -> str:
    def _replace_highlight(s: str) -> str:
        s = re.sub(r"(?<=[^\s])<hi>(.*?)</hi>", r"\1", s)
        s = s.replace("</hi>", "*").replace("<hi>", "*")
        return s

    final_matches = [
        replace_whitespaces_w_space(_replace_highlight(match_str)).strip()
        for match_str in match_strs
        if match_str
    ]
    combined = "... ".join(final_matches)

    # Slack introduces "Show More" after 300 on desktop which is ugly
    # But don't trim the message if there is still a highlight after 300 chars
    remaining = 300 - used_chars
    if len(combined) > remaining and "*" not in combined[remaining:]:
        combined = combined[: remaining - 3] + "..."

    return combined


def remove_slack_text_interactions(slack_str: str) -> str:
    slack_str = SlackTextCleaner.replace_tags_basic(slack_str)
    slack_str = SlackTextCleaner.replace_channels_basic(slack_str)
    slack_str = SlackTextCleaner.replace_special_mentions(slack_str)
    slack_str = SlackTextCleaner.replace_links(slack_str)
    slack_str = SlackTextCleaner.replace_special_catchall(slack_str)
    slack_str = SlackTextCleaner.add_zero_width_whitespace_after_tag(slack_str)
    return slack_str


def get_channel_from_id(client: WebClient, channel_id: str) -> dict[str, Any]:
    response = client.conversations_info(channel=channel_id)
    response.validate()
    return response["channel"]


def get_channel_name_from_id(
    client: WebClient, channel_id: str
) -> tuple[str | None, bool]:
    try:
        channel_info = get_channel_from_id(client, channel_id)
        name = channel_info.get("name")
        is_dm = any([channel_info.get("is_im"), channel_info.get("is_mpim")])
        return name, is_dm
    except SlackApiError as e:
        logger.exception(f"Couldn't fetch channel name from id: {channel_id}")
        raise e


def fetch_userids_from_emails(user_emails: list[str], client: WebClient) -> list[str]:
    user_ids: list[str] = []
    for email in user_emails:
        try:
            user = client.users_lookupByEmail(email=email)
            user_ids.append(user.data["user"]["id"])  # type: ignore
        except Exception:
            logger.error(f"Was not able to find slack user by email: {email}")

    if not user_ids:
        raise RuntimeError(
            "Was not able to find any Slack users to respond to. "
            "No email was parsed into a valid slack account."
        )

    return user_ids


def fetch_user_semantic_id_from_id(user_id: str, client: WebClient) -> str | None:
    response = client.users_info(user=user_id)
    if not response["ok"]:
        return None

    user: dict = cast(dict[Any, dict], response.data).get("user", {})

    return (
        user.get("real_name")
        or user.get("name")
        or user.get("profile", {}).get("email")
    )


def read_slack_thread(
    channel: str, thread: str, client: WebClient
) -> list[ThreadMessage]:
    thread_messages: list[ThreadMessage] = []
    response = client.conversations_replies(channel=channel, ts=thread)
    replies = cast(dict, response.data).get("messages", [])
    for reply in replies:
        if "user" in reply and "bot_id" not in reply:
            message = remove_danswer_bot_tag(reply["text"], client=client)
            user_sem_id = fetch_user_semantic_id_from_id(reply["user"], client)
            message_type = MessageType.USER
        else:
            self_app_id = get_danswer_bot_app_id(client)

            # Only include bot messages from Danswer, other bots are not taken in as context
            if self_app_id != reply.get("user"):
                continue

            blocks = reply["blocks"]
            if len(blocks) <= 1:
                continue

            # The useful block is the second one after the header block that says AI Answer
            message = reply["blocks"][1]["text"]["text"]

            if message.startswith("_Filters"):
                if len(blocks) <= 2:
                    continue
                message = reply["blocks"][2]["text"]["text"]

            user_sem_id = "Assistant"
            message_type = MessageType.ASSISTANT

        thread_messages.append(
            ThreadMessage(message=message, sender=user_sem_id, role=message_type)
        )

    return thread_messages
