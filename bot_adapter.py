from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, ActivityTypes, Attachment, OAuthCard, ActionTypes, CardAction
from aiohttp import ClientSession, web
import os
import secrets
from agent.handler import (
    handle_application_form_upload,
    handle_cv_upload,
    handle_form_uploaded,
    handle_graph_token_response,
    handle_message,
)
from agent.profile import get_profile
from dotenv import load_dotenv

# Load local .env file if running locally
load_dotenv()

# Securely fetch all required parameters
APP_ID = os.environ.get("MicrosoftAppId", os.environ.get("BOT_APP_ID", ""))
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", os.environ.get("BOT_APP_PASSWORD", ""))
TENANT_ID = os.environ.get("MicrosoftAppTenantId", "")

SETTINGS = BotFrameworkAdapterSettings(
    app_id=APP_ID,
    app_password=APP_PASSWORD,
    channel_auth_tenant=TENANT_ID
)

ADAPTER = BotFrameworkAdapter(SETTINGS)
DOWNLOAD_STORE = {}


def _register_download(file_bytes: bytes, filename: str, content_type: str) -> str:
    token = secrets.token_urlsafe(16)
    DOWNLOAD_STORE[token] = {
        "bytes": file_bytes,
        "filename": filename,
        "content_type": content_type,
    }
    return token


def _public_base_url() -> str:
    explicit = os.environ.get("BOT_PUBLIC_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("PORT", "3978")
    return f"http://localhost:{port}".rstrip("/")


def _download_url_warning(url: str) -> str:
    if "localhost" in url or "127.0.0.1" in url:
        return (
            "\n\n_Note: downloads use a local URL and will not work from Copilot unless "
            "BOT_PUBLIC_URL is set to your deployed bot HTTPS address._"
        )
    return ""


async def download_file(req: web.Request) -> web.Response:
    token = req.match_info.get("token", "")
    entry = DOWNLOAD_STORE.get(token)
    if not entry:
        return web.Response(status=404, text="File not found or expired")

    return web.Response(
        body=entry["bytes"],
        headers={
            "Content-Type": entry["content_type"],
            "Content-Disposition": f'attachment; filename="{entry["filename"]}"',
        },
    )


async def messages(req: web.Request) -> web.Response:
    if not req.can_read_body:
        return web.Response(status=400, text="Missing request body")

    body = await req.json()
    activity = Activity().deserialize(body)
    auth = req.headers.get("Authorization", "")

    async def turn_handler(turn_context: TurnContext):
        from_property = turn_context.activity.from_property
        student_id = from_property.id if from_property and from_property.id else None

        if not student_id:
            await turn_context.send_activity("I couldn't identify your user ID.")
            return

        async def send_oauth_card(oauth_payload: dict):
            card_text = oauth_payload.get(
                "text",
                "Please sign in with your Microsoft account to continue.",
            )
            card = OAuthCard(
                text=card_text,
                connection_name=oauth_payload.get("connection_name", "GraphOAuth"),
                buttons=[
                    CardAction(
                        type=ActionTypes.signin,
                        title="Sign In",
                        value="signin",
                    )
                ],
            )
            attachment = Attachment(
                content_type="application/vnd.microsoft.card.oauth",
                content=card,
            )
            await turn_context.send_activity(Activity(
                type=ActivityTypes.message,
                text=card_text,
                attachments=[attachment],
            ))

        async def send_single_response(response: dict):
            attachments = []
            if response.get("attachments"):
                for att in response["attachments"]:
                    attachments.append(Attachment(
                        content_type=att.get("contentType"),
                        content=att.get("content")
                    ))

            file_download = response.get("file_download")
            message_text = response.get("text", "") or ""
            if file_download:
                token = _register_download(
                    file_download["bytes"],
                    file_download["filename"],
                    file_download["content_type"],
                )
                download_url = f"{_public_base_url()}/download/{token}"
                message_text = f"{message_text}\n\n**Download:** {download_url}{_download_url_warning(download_url)}".strip()
                attachments.append(Attachment(
                    content_type=file_download["content_type"],
                    content_url=download_url,
                    name=file_download["filename"],
                ))

            await turn_context.send_activity(Activity(
                type="message",
                text=message_text,
                attachments=attachments if attachments else None
            ))

        async def send_agent_responses(responses):
            if isinstance(responses, dict):
                if responses.get("type") == "oauth_login_required":
                    await send_oauth_card(responses)
                return

            for response in responses or []:
                if isinstance(response, dict) and response.get("type") == "oauth_login_required":
                    await send_oauth_card(response)
                    continue
                await send_single_response(response)

        activity_type = turn_context.activity.type
        activity_name = getattr(turn_context.activity, "name", "") or ""

        if activity_type == ActivityTypes.event and activity_name in ("tokens/response", "token/response"):
            token_payload = turn_context.activity.value or {}
            await send_agent_responses(handle_graph_token_response(student_id, token_payload))
            return

        if activity_type != ActivityTypes.message:
            return

        channel_data = turn_context.activity.channel_data or {}
        if not isinstance(channel_data, dict):
            channel_data = {}
        event_type = str(channel_data.get("eventType") or channel_data.get("event_type") or "").lower()
        activity_name_lower = activity_name.lower()
        is_edit = (
            "edit" in event_type
            or "update" in event_type
            or "edit" in activity_name_lower
            or "update" in activity_name_lower
        )

        def is_supported_document(att) -> bool:
            content_type = (getattr(att, "content_type", "") or "").lower()
            filename = (getattr(att, "name", "") or "").lower()
            return (
                content_type == "application/pdf"
                or content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                or filename.endswith(".pdf")
                or filename.endswith(".docx")
            )

        async def download_document_attachment(att) -> tuple[bytes, str, str]:
            content_url = getattr(att, "content_url", None)
            filename = getattr(att, "name", None) or "upload.pdf"
            content_type = getattr(att, "content_type", None) or ""

            if not content_url:
                raise ValueError("Attachment is missing a content URL")

            headers = {"Authorization": auth} if auth else {}
            async with ClientSession(headers=headers) as session:
                async with session.get(content_url) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"Could not download attachment: HTTP {resp.status}")
                    file_bytes = await resp.read()

            return file_bytes, filename, content_type

        document_attachments = []
        for att in turn_context.activity.attachments or []:
            if is_supported_document(att):
                document_attachments.append(att)

        if document_attachments:
            profile = get_profile(student_id)
            app_state = (profile or {}).get("application_state") or {}
            has_pending_application = bool(profile and profile.get("pending_application"))
            has_active_collection = app_state.get("step") == "collecting_list"
            has_active_draft = bool(profile and profile.get("last_scholarship_id"))

            await turn_context.send_activity(
                "📄 Processing your application form... please wait."
                if has_pending_application or has_active_collection or has_active_draft else
                "📄 Processing your CV... please wait."
            )

            for att in document_attachments:
                try:
                    file_bytes, filename, content_type = await download_document_attachment(att)
                    if has_pending_application:
                        await send_agent_responses(handle_application_form_upload(
                            student_id, file_bytes, filename, content_type
                        ))
                    elif has_active_draft:
                        await send_agent_responses(handle_form_uploaded(student_id, file_bytes, filename))
                    else:
                        await send_agent_responses(handle_cv_upload(student_id, file_bytes, filename))
                except Exception as e:
                    await turn_context.send_activity(f"Sorry, I couldn't process that attachment: {e}")
            return

        message = {
            "type": turn_context.activity.type,
            "text": turn_context.activity.text or "",
            "value": turn_context.activity.value or {},
            "is_edit": is_edit,
            "event_type": event_type,
            "activity_name": activity_name_lower
        }

        responses = handle_message(student_id, message)
        await send_agent_responses(responses)

    try:
        await ADAPTER.process_activity(activity, auth, turn_handler)
        return web.Response(status=200)
    except Exception as e:
        print(f"Error during process_activity: {str(e)}")
        return web.Response(status=500, text="Internal server error")

app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/download/{token}", download_file)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3978))
    web.run_app(app, host="0.0.0.0", port=port)
