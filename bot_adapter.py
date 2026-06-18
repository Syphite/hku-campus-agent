from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity
from aiohttp import web
import os
import json
from agent.handler import handle_message
from dotenv import load_dotenv

# Load local .env file if running locally (Azure will use its Configuration Settings)
load_dotenv()

# Securely fetch all required parameters for a cross-tenant / multi-tenant bot
APP_ID = os.environ.get("MicrosoftAppId", os.environ.get("BOT_APP_ID", ""))
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", os.environ.get("BOT_APP_PASSWORD", ""))
TENANT_ID = os.environ.get("MicrosoftAppTenantId", "common")  # Defaults to 'common' for MultiTenant

# Explicitly pass the configuration using correct parameter names
SETTINGS = BotFrameworkAdapterSettings(
    app_id=APP_ID,
    app_password=APP_PASSWORD,
    channel_auth_tenant=TENANT_ID  # Correct parameter name for tenant
)

ADAPTER = BotFrameworkAdapter(SETTINGS)

async def messages(req: web.Request) -> web.Response:
    # Check for empty body to prevent JSON parsing crashes
    if not req.has_body:
        return web.Response(status=400, text="Missing request body")
        
    body     = await req.json()
    activity = Activity().deserialize(body)
    auth     = req.headers.get("Authorization", "")

    async def turn_handler(turn_context: TurnContext):
        # Extract student metadata safely
        student_id = turn_context.activity.from_property.id if turn_context.activity.from_property else "unknown_user"
        
        message = {
            "type":  turn_context.activity.type,
            "text":  turn_context.activity.text or "",
            "value": turn_context.activity.value or {}
        }
        
        # Process logic via your custom agent handler
        responses = handle_message(student_id, message)
        
        # Iteratively send responses back to the channel
        for r in responses:
            await turn_context.send_activity(Activity(
                type="message",
                text=r.get("text", ""),
                attachments=r.get("attachments", [])
            ))

    # Process the incoming pipeline activity
    try:
        await ADAPTER.process_activity(activity, auth, turn_handler)
        return web.Response(status=200)
    except Exception as e:
        # Prevent completely silent failures inside the aiohttp thread
        print(f"Error during process_activity: {str(e)}")
        return web.Response(status=500, text="Internal server error processing bot activity")

app = web.Application()
app.router.add_post("/api/messages", messages)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3978))
    web.run_app(app, host="0.0.0.0", port=port)
