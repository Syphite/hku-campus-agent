import os
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, url_for, session

from agent.graph      import get_unread_emails, archive_email, is_protected_sender
from agent.classifier import classify_email, suggested_course_keywords

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

USE_GRAPH = os.getenv("USE_GRAPH", "0") == "1"
GRAPH_ACCESS_TOKEN = os.getenv("GRAPH_ACCESS_TOKEN", "")
AUTO_ARCHIVE = os.getenv("AUTO_ARCHIVE", "0") == "1"

DEMO_EMAILS = [
    {
        "id": "1",
        "subject": "URGENT: Assignment 2 submission deadline tomorrow 11:59pm",
        "from": "prof.chan@hku.hk",
        "bodyPreview": "Dear students, please note that Assignment 2 is due tomorrow at 11:59pm. Late submissions will not be accepted."
    },
    {
        "id": "2",
        "subject": "HKU Career Fair 2024 - Register Now!",
        "from": "careers@hku.hk",
        "bodyPreview": "Join us for the annual HKU Career Fair on November 15th. Over 100 companies will be present including Google, HSBC and more."
    },
    {
        "id": "3",
        "subject": "50% OFF Pizza delivery this weekend only!",
        "from": "promo@foodpanda.com",
        "bodyPreview": "This weekend only - get 50% off your first 3 orders. Use code WEEKEND50 at checkout."
    },
    {
        "id": "4",
        "subject": "Your exam timetable for Semester 1 2024",
        "from": "registry@hku.hk",
        "bodyPreview": "Dear student, your final exam timetable has been confirmed. Please log in to the portal to view your schedule."
    },
    {
        "id": "5",
        "subject": "HKU Scholarship Application Now Open",
        "from": "scholarships@hku.hk",
        "bodyPreview": "Applications for the HKU Merit Scholarship 2024-25 are now open. Eligible students with GPA above 3.5 can apply before December 1st."
    },
    {
        "id": "6",
        "subject": "ASSO_FORUM: Looking for groupmates for COMP project",
        "from": "asso_forum@hku.hk",
        "bodyPreview": "We are looking for 2 more groupmates for a computer science project on recommendation systems."
    },
    {
        "id": "7",
        "subject": "ASSO_FORUM: Selling used textbooks",
        "from": "asso_forum@hku.hk",
        "bodyPreview": "Used textbooks for sale: economics, accounting, and general education materials."
    },
    {
        "id": "8",
        "subject": "Reminder: Library books due for return",
        "from": "library@hku.hk",
        "bodyPreview": "You have 2 books due for return this Friday. Please return or renew them to avoid fines."
    }
]

ONBOARDING_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Student Email Agent - Setup</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 920px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; margin-bottom: 8px; }
        .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 8px rgba(0,0,0,0.08); }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        label { display: block; font-size: 13px; color: #555; margin-bottom: 6px; }
        input, select, textarea {
            width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px;
            font-size: 14px; box-sizing: border-box; background: #fff;
        }
        textarea { min-height: 92px; resize: vertical; }
        .section { margin-top: 16px; }
        .checks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
        .check-item { padding: 8px 10px; border: 1px solid #e5e5e5; border-radius: 8px; background: #fafafa; }
        .actions { margin-top: 18px; display: flex; gap: 10px; }
        button, .btn {
            display: inline-block; border: 0; border-radius: 10px; padding: 12px 16px;
            font-weight: 700; cursor: pointer; text-decoration: none;
        }
        button { background: #222; color: white; }
        .hint { color: #777; font-size: 13px; margin-top: 6px; }
        .small { color: #888; font-size: 13px; }
    </style>
</head>
<body>
    <h1>📧 Student Email Agent</h1>
    <p class="small">Tell us what you care about, and we will rank emails by your year, major, and interests.</p>

    <div class="card">
        <form method="post" action="{{ url_for('save_profile') }}">
            <div class="grid">
                <div>
                    <label for="year">Year</label>
                    <select id="year" name="year" required>
                        <option value="">Select year</option>
                        <option>Year 1</option>
                        <option>Year 2</option>
                        <option>Year 3</option>
                        <option>Year 4</option>
                        <option>Master</option>
                        <option>PhD</option>
                    </select>
                </div>

                <div>
                    <label for="major">Major</label>
                    <input id="major" name="major" type="text" placeholder="e.g. Computer Science" required>
                </div>
            </div>

            <div class="section">
                <label>Interests</label>
                <div class="checks">
                    {% for item in interests %}
                    <label class="check-item">
                        <input type="checkbox" name="interests" value="{{ item }}"> {{ item }}
                    </label>
                    {% endfor %}
                </div>
                <div class="hint">You can select more than one.</div>
            </div>

            <div class="section">
                <label for="custom_interests">Custom interests</label>
                <textarea id="custom_interests" name="custom_interests" placeholder="Add more interests separated by commas, e.g. AI, fintech, product management"></textarea>
            </div>

            <div class="section">
                <label for="courses">Known / preferred courses</label>
                <textarea id="courses" name="courses" placeholder="Optional: add course names or course keywords separated by commas"></textarea>
                <div class="hint">If empty, the app will guess likely course keywords from your major and year.</div>
            </div>

            <div class="section">
                <label class="check-item" style="display:inline-flex; gap:8px; align-items:center; width:auto;">
                    <input type="checkbox" name="include_asso_forum" checked>
                    Include ASSO_FORUM posts
                </label>
            </div>

            <div class="actions">
                <button type="submit">Start filtering</button>
            </div>
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Student Email Agent</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 980px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; }
        .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
        .btn-link {
            background: #222; color: white; text-decoration: none; padding: 10px 14px; border-radius: 10px;
            display: inline-block; font-weight: 700;
        }
        .card, .stats {
            background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 8px rgba(0,0,0,0.08);
        }
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 16px 0 20px; }
        .stat { text-align: center; }
        .stat-num { font-size: 28px; font-weight: 800; }
        .stat-label { font-size: 13px; color: #666; }
        .email-card { background: white; border-radius: 10px; padding: 16px; margin: 12px 0; border-left: 5px solid #ccc; box-shadow: 0 1px 5px rgba(0,0,0,0.05); }
        .urgent { border-left-color: #e74c3c; }
        .relevant { border-left-color: #2ecc71; }
        .noise { border-left-color: #95a5a6; opacity: 0.7; }
        .ambiguous { border-left-color: #f39c12; }
        .label {
            display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 800;
            color: white; margin-bottom: 8px;
        }
        .label-urgent { background: #e74c3c; }
        .label-relevant { background: #2ecc71; }
        .label-noise { background: #95a5a6; }
        .label-ambiguous { background: #f39c12; }
        .from { font-size: 13px; color: #666; }
        .subject { font-weight: 700; margin: 6px 0; }
        .reason { font-size: 13px; color: #666; font-style: italic; }
        .section-title { margin-top: 26px; }
        .meta { color: #777; font-size: 13px; }
        .tags { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
        .tag { background: #f0f0f0; border-radius: 999px; padding: 6px 10px; font-size: 12px; color: #333; }
        .archived-section { margin-top: 30px; }
    </style>
</head>
<body>
    <div class="topbar">
        <div>
            <h1>📧 Student Email Digest</h1>
            <div class="meta">
                {{ total }} emails processed · {{ archived }} auto-archived
                {% if source_mode %} · source: {{ source_mode }}{% endif %}
            </div>
        </div>
        <a class="btn-link" href="{{ url_for('reset_profile') }}">Edit profile</a>
    </div>

    <div class="card" style="margin-top:16px;">
        <strong>Profile</strong>
        <div class="meta">Year: {{ profile.year }} · Major: {{ profile.major }}</div>
        <div class="tags">
            {% for item in profile.interests %}
            <span class="tag">{{ item }}</span>
            {% endfor %}
        </div>

        <div style="margin-top:14px;">
            <strong>Likely course keywords</strong>
            <div class="tags">
                {% for item in course_hints %}
                <span class="tag">{{ item }}</span>
                {% endfor %}
            </div>
        </div>
    </div>

    <div class="stats">
        <div class="stat"><div class="stat-num" style="color:#e74c3c">{{ urgent }}</div><div class="stat-label">Urgent</div></div>
        <div class="stat"><div class="stat-num" style="color:#2ecc71">{{ relevant }}</div><div class="stat-label">Relevant</div></div>
        <div class="stat"><div class="stat-num" style="color:#f39c12">{{ ambiguous }}</div><div class="stat-label">Ambiguous</div></div>
        <div class="stat"><div class="stat-num" style="color:#95a5a6">{{ archived }}</div><div class="stat-label">Auto-archived</div></div>
    </div>

    <h2 class="section-title">📌 Important emails</h2>
    {% for email in important_emails %}
    <div class="email-card {{ email.label }}">
        <span class="label label-{{ email.label }}">{{ email.label.upper() }}</span>
        <div class="from">From: {{ email.from }}</div>
        <div class="subject">{{ email.subject }}</div>
        <div class="reason">🤖 {{ email.reason }}</div>
    </div>
    {% endfor %}

    <div class="archived-section">
        <h2 class="section-title">🗄️ Auto-archived (noise)</h2>
        {% for email in noise_emails %}
        <div class="email-card noise">
            <span class="label label-noise">NOISE</span>
            <div class="from">From: {{ email.from }}</div>
            <div class="subject">{{ email.subject }}</div>
            <div class="reason">🤖 {{ email.reason }}</div>
        </div>
        {% endfor %}
    </div>
</body>
</html>
"""


def normalize_email_sender(email):
    sender = email.get("from", "")
    if isinstance(sender, dict):
        return sender.get("emailAddress", {}).get("address", "")
    return sender or ""


def normalize_email_subject(email):
    return email.get("subject", "") or ""


def normalize_email_body(email):
    return email.get("bodyPreview", "") or ""


def get_demo_or_graph_emails():
    if USE_GRAPH and GRAPH_ACCESS_TOKEN:
        try:
            return get_unread_emails(GRAPH_ACCESS_TOKEN), "Microsoft Graph"
        except Exception:
            # Fallback to demo data if Graph fails
            return DEMO_EMAILS, "Demo fallback"
    return DEMO_EMAILS, "Demo data"


def current_profile():
    return session.get("student_profile")


@app.route("/", methods=["GET"])
def index():
    profile = current_profile()
    if not profile:
        return render_template_string(
            ONBOARDING_TEMPLATE,
            interests=[
                "Career", "Internship", "Networking", "Research",
                "Scholarship", "Hackathon", "Student events", "Groupmates"
            ]
        )

    emails, source_mode = get_demo_or_graph_emails()
    important_emails = []
    noise_emails = []

    for email in emails:
        sender = normalize_email_sender(email)
        subject = normalize_email_subject(email)
        body_preview = normalize_email_body(email)

        classification = classify_email(subject, body_preview, sender, profile)
        label = classification.get("label", "ambiguous")
        reason = classification.get("reason", "")

        email_data = {
            "label": label,
            "subject": subject,
            "from": sender,
            "reason": reason,
            "id": email.get("id")
        }

        if label == "noise":
            noise_emails.append(email_data)

            if USE_GRAPH and AUTO_ARCHIVE and GRAPH_ACCESS_TOKEN and email.get("id") and not is_protected_sender(sender):
                try:
                    archive_email(email["id"], GRAPH_ACCESS_TOKEN)
                except Exception:
                    pass
        else:
            important_emails.append(email_data)

    urgent = sum(1 for e in important_emails if e["label"] == "urgent")
    relevant = sum(1 for e in important_emails if e["label"] == "relevant")
    ambiguous = sum(1 for e in important_emails if e["label"] == "ambiguous")

    return render_template_string(
        DASHBOARD_TEMPLATE,
        profile=profile,
        course_hints=suggested_course_keywords(profile),
        important_emails=important_emails,
        noise_emails=noise_emails,
        urgent=urgent,
        relevant=relevant,
        ambiguous=ambiguous,
        archived=len(noise_emails),
        total=len(emails),
        source_mode=source_mode
    )


@app.route("/save-profile", methods=["POST"])
def save_profile():
    interests = request.form.getlist("interests")
    custom_interests_raw = request.form.get("custom_interests", "")
    courses_raw = request.form.get("courses", "")

    custom_interests = [
        x.strip() for x in custom_interests_raw.split(",") if x.strip()
    ]
    courses = [
        x.strip() for x in courses_raw.split(",") if x.strip()
    ]

    profile = {
        "year": request.form.get("year", "").strip(),
        "major": request.form.get("major", "").strip(),
        "interests": sorted(set([x.strip() for x in interests if x.strip()] + custom_interests)),
        "courses": courses,
        "include_asso_forum": request.form.get("include_asso_forum") == "on",
    }

    session["student_profile"] = profile
    return redirect(url_for("index"))


@app.route("/reset")
def reset_profile():
    session.pop("student_profile", None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)