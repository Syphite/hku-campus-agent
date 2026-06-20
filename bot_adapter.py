from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, Attachment
from aiohttp import ClientSession, web
import os
from agent.handler import handle_cv_upload, handle_form_uploaded, handle_message
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

async def messages(req: web.Request) -> web.Response:
    if not req.can_read_body:
        return web.Response(status=400, text="Missing request body")

    body = await req.json()
    activity = Activity().deserialize(body)
    auth = req.headers.get("Authorization", "")

    async def turn_handler(turn_context: TurnContext):
        if turn_context.activity.type != "message":
            return

        channel_data = turn_context.activity.channel_data or {}
        if not isinstance(channel_data, dict):
            channel_data = {}
        event_type = str(channel_data.get("eventType") or channel_data.get("event_type") or "").lower()
        activity_name = str(getattr(turn_context.activity, "name", "") or "").lower()
        is_edit = (
            "edit" in event_type
            or "update" in event_type
            or "edit" in activity_name
            or "update" in activity_name
        )

        from_property = turn_context.activity.from_property
        student_id = from_property.id if from_property and from_property.id else None

        if not student_id:
            await turn_context.send_activity("I couldn't identify your user ID.")
            return

        async def send_agent_responses(responses):
            for r in responses:
                attachments = []
                if r.get("attachments"):
                    for att in r["attachments"]:
                        attachments.append(Attachment(
                            content_type=att.get("contentType"),
                            content=att.get("content")
                        ))

                await turn_context.send_activity(Activity(
                    type="message",
                    text=r.get("text", ""),
                    attachments=attachments if attachments else None
                ))

        def is_supported_document(att) -> bool:
            content_type = (getattr(att, "content_type", "") or "").lower()
            filename = (getattr(att, "name", "") or "").lower()
            return (
                "pdf" in content_type
                or "wordprocessingml.document" in content_type
                or filename.endswith(".pdf")
                or filename.endswith(".docx")
            )

        async def download_document_attachment(att) -> tuple[bytes, str]:
            content_url = getattr(att, "content_url", None)
            filename = getattr(att, "name", None) or "upload.pdf"

            if not content_url:
                raise ValueError("Attachment is missing a content URL")

            headers = {"Authorization": auth} if auth else {}
            async with ClientSession(headers=headers) as session:
                async with session.get(content_url) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"Could not download attachment: HTTP {resp.status}")
                    file_bytes = await resp.read()

            return file_bytes, filename

        document_attachments = []
        for att in turn_context.activity.attachments or []:
            if is_supported_document(att):
                document_attachments.append(att)

        if document_attachments:
            profile = get_profile(student_id)
            has_active_draft = bool(profile and profile.get("last_scholarship_id"))

            await turn_context.send_activity(
                "📄 Processing your application form... please wait."
                if has_active_draft else
                "📄 Processing your CV... please wait."
            )

            for att in document_attachments:
                try:
                    file_bytes, filename = await download_document_attachment(att)
                    if has_active_draft:
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
            "activity_name": activity_name
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3978))
    web.run_app(app, host="0.0.0.0", port=port)
