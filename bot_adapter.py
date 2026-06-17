from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity
from aiohttp import web
import os, json
from agent.handler import handle_message
from dotenv import load_dotenv

load_dotenv()
SETTINGS = BotFrameworkAdapterSettings(
    app_id=os.environ.get("BOT_APP_ID", ""),
    app_password=os.environ.get("BOT_APP_PASSWORD", "")
)
ADAPTER = BotFrameworkAdapter(SETTINGS)

async def messages(req: web.Request) -> web.Response:
    body     = await req.json()
    activity = Activity().deserialize(body)
    auth     = req.headers.get("Authorization", "")

    async def turn_handler(turn_context: TurnContext):
        student_id = turn_context.activity.from_property.id
        message    = {
            "type":  turn_context.activity.type,
            "text":  turn_context.activity.text or "",
            "value": turn_context.activity.value or {}
        }
        responses = handle_message(student_id, message)
        for r in responses:
            await turn_context.send_activity(Activity(
                type="message",
                text=r.get("text", ""),
                attachments=r.get("attachments", [])
            ))

    await ADAPTER.process_activity(activity, auth, turn_handler)
    return web.Response(status=200)

app = web.Application()
app.router.add_post("/api/messages", messages)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 3978))
    web.run_app(app, host="0.0.0.0", port=port)
