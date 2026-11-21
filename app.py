import hmac
import json
import re
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from openai import OpenAI
from supabase import create_client
from twilio.rest import Client as TwilioClient


st.set_page_config(
    page_title="استباق | الإنذار المبكر",
    page_icon="🚨",
    layout="wide",
)

st.markdown(
    """
    <style>
    .main, .stTextInput, .stTextArea, .stSelectbox, .stButton {
        direction: rtl;
        text-align: right;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = get_secret("SUPABASE_SERVICE_ROLE_KEY")
TWILIO_ACCOUNT_SID = get_secret("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = get_secret("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = get_secret(
    "TWILIO_WHATSAPP_FROM",
    "whatsapp:+14155238886",
)
MANAGER_WHATSAPP = get_secret("MANAGER_WHATSAPP")
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")


LOCATIONS = {
    "عرعر - حي المباركية": (30.9753, 41.0381),
    "عرعر - الفرع الرئيسي": (30.9833, 41.0167),
    "الرياض - فرع العليا": (24.7136, 46.6753),
    "جدة - فرع الشمال": (21.5433, 39.1728),
}

SYSTEM_PROMPT = """
حلل ملاحظة مستفيد من جهة خدمية في السعودية.
أخرج JSON صالحًا فقط، بهذه المفاتيح:
{
  "sentiment": "غاضب_جداً أو سلبي أو محايد أو إيجابي",
  "urgency_score": 1,
  "root_cause": "سبب مختصر",
  "department": "تقنية المعلومات أو خدمة المستفيدين أو العمليات الميدانية أو الإدارة المالية",
  "executive_action": "إجراء مقترح مختصر",
  "pr_draft": null
}
اجعل urgency_score رقمًا صحيحًا من 1 إلى 10.
لا تذكر أسماء أشخاص أو أرقامًا أو بيانات تعريفية.
"""


def redact_basic_pii(text):
    """يحجب أنماطًا شائعة من أرقام الجوال والهوية والبريد الإلكتروني."""
    digit_map = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    )
    text = str(text).translate(digit_map)
    text = re.sub(
        r"(?<!\d)(?:(?:\+?966|00966)\s*0?|0)?5\d{8}(?!\d)",
        "[رقم جوال محذوف]",
        text,
    )
    text = re.sub(
        r"(?<!\d)[12]\d{9}(?!\d)",
        "[رقم هوية محتمل محذوف]",
        text,
    )
    text = re.sub(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        "[بريد إلكتروني محذوف]",
        text,
    )
    return text


@st.cache_resource
def get_supabase_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def analyze_with_ai(text, source, location):
    if not OPENAI_API_KEY:
        raise ValueError("مفتاح OpenAI غير مضبوط في إعدادات التطبيق.")

    safe_text = redact_basic_pii(text)
    client = OpenAI(api_key=OPENAI_API_KEY)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"المصدر: {source}\n"
                    f"الموقع: {location}\n"
                    f"الملاحظة بعد حجب الأرقام الشائعة: {safe_text}"
                ),
            },
        ],
        temperature=0.1,
    )

    content = response.choices[0].message.content or "{}"
    result = json.loads(content)

    sentiment = str(result.get("sentiment", "محايد"))
    allowed_sentiments = {"غاضب_جداً", "سلبي", "محايد", "إيجابي"}
    if sentiment not in allowed_sentiments:
        sentiment = "محايد"

    try:
        urgency_score = int(result.get("urgency_score", 1))
    except (TypeError, ValueError):
        urgency_score = 1
    urgency_score = max(1, min(10, urgency_score))

    department = str(result.get("department", "خدمة المستفيدين"))
    allowed_departments = {
        "تقنية المعلومات",
        "خدمة المستفيدين",
        "العمليات الميدانية",
        "الإدارة المالية",
    }
    if department not in allowed_departments:
        department = "خدمة المستفيدين"

    return {
        "sanitized_text": redact_basic_pii(
            str(result.get("sanitized_text") or safe_text)
        ),
        "sentiment": sentiment,
        "urgency_score": urgency_score,
        "root_cause": str(result.get("root_cause", "يحتاج إلى مراجعة")),
        "department": department,
        "early_warning": urgency_score >= 8 or sentiment == "غاضب_جداً",
        "executive_action": str(
            result.get("executive_action", "مراجعة الحالة ومعالجة سببها.")
        ),
        "pr_draft": result.get("pr_draft"),
    }


def send_whatsapp_alert(data, location):
    if not (
        TWILIO_ACCOUNT_SID
        and TWILIO_AUTH_TOKEN
        and MANAGER_WHATSAPP
        and TWILIO_WHATSAPP_FROM
    ):
        return False, "إعدادات Twilio غير مكتملة."

    to_number = MANAGER_WHATSAPP
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    from_number = TWILIO_WHATSAPP_FROM
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"

    message_body = (
        "إنذار مبكر عاجل - منصة استباق\n"
        f"الموقع: {location}\n"
        f"الإدارة: {data['department']}\n"
        f"السبب: {data['root_cause']}\n"
        f"درجة الخطورة: {data['urgency_score']}/10\n"
        f"الإجراء المقترح: {data['executive_action']}"
    )

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            from_=from_number,
            to=to_number,
            body=message_body,
        )
        return True, message.sid
    except Exception:
        return False, "تعذر إرسال الرسالة. راجع إعدادات Twilio وحالة الرسالة."


def require_admin_login():
    if not ADMIN_PASSWORD:
        st.error("أضف ADMIN_PASSWORD إلى إعدادات Secrets قبل فتح لوحة الإدارة.")
        st.stop()

    if st.session_state.get("admin_authenticated"):
        return

    with st.form("admin_login"):
        password = st.text_input("كلمة مرور لوحة الإدارة", type="password")
        submitted = st.form_submit_button("دخول")

    if submitted:
        if hmac.compare_digest(password, ADMIN_PASSWORD):
            st.session_state.admin_authenticated = True
            st.rerun()
        st.error("كلمة المرور غير صحيحة.")

    st.stop()


supabase = get_supabase_client()

st.sidebar.title("منصة استباق")
page = st.sidebar.radio(
    "اختر الصفحة",
    ["إرسال ملاحظة", "لوحة الإدارة"],
)

if page == "إرسال ملاحظة":
    st.title("صوتك مسموع")
    st.write("اكتب ملاحظتك عن تجربتك مع الخدمة.")

    st.caption(
        "لا تكتب اسمًا أو رقم هوية أو رقم جوال. "
        "يحجب التطبيق بعض أنماط الأرقام والبريد الإلكتروني آليًا."
    )

    with st.form("feedback_form"):
        source = st.selectbox(
            "قناة التفاعل",
            ["رمز QR الميداني", "منصة X", "واتساب خدمة العملاء", "مركز الاتصال"],
        )
        location = st.selectbox("الفرع أو المدينة", list(LOCATIONS.keys()))
        user_text = st.text_area("ملاحظتك")
        submitted = st.form_submit_button("إرسال الملاحظة")

    if submitted:
        if not user_text.strip():
            st.error("اكتب الملاحظة أولًا.")
        elif not OPENAI_API_KEY:
            st.error("إعداد مفتاح OpenAI غير مكتمل.")
        elif supabase is None:
            st.error("إعداد اتصال Supabase غير مكتمل.")
        else:
            try:
                with st.spinner("يجري تحليل الملاحظة وحفظها..."):
                    result = analyze_with_ai(user_text, source, location)
                    latitude, longitude = LOCATIONS[location]

                    record = {
                        "source_type": source,
                        "location_name": location,
                        "latitude": latitude,
                        "longitude": longitude,
                        "sanitized_text": result["sanitized_text"],
                        "sentiment": result["sentiment"],
                        "urgency_score": result["urgency_score"],
                        "root_cause": result["root_cause"],
                        "department": result["department"],
                        "early_warning": result["early_warning"],
                        "executive_action": result["executive_action"],
                        "pr_draft": result["pr_draft"],
                    }

                    supabase.table("estibaq_records").insert(record).execute()

                st.success("تم حفظ الملاحظة وتحليلها.")

                if result["early_warning"]:
                    sent, message = send_whatsapp_alert(result, location)
                    if sent:
                        st.warning(
                            "سُلّم طلب إرسال التنبيه إلى Twilio. "
                            f"رقم الرسالة: {message}"
                        )
                    else:
                        st.info(f"حُفظ الإنذار. حالة واتساب: {message}")

            except Exception:
                st.error(
                    "تعذر إكمال التحليل أو الحفظ. "
                    "تحقق من إعدادات OpenAI وSupabase وجدول قاعدة البيانات."
                )


elif page == "لوحة الإدارة":
    require_admin_login()

    if st.sidebar.button("تسجيل الخروج"):
        st.session_state.admin_authenticated = False
        st.rerun()

    st.title("لوحة الإنذار المبكر")

    if supabase is None:
        st.error("إعداد اتصال Supabase غير مكتمل.")
        st.stop()

    try:
        response = (
            supabase.table("estibaq_records")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        records = response.data or []
    except Exception:
        st.error("تعذر قراءة السجلات. تحقق من إعداد الجدول في Supabase.")
        st.stop()

    if not records:
        st.info("لا توجد ملاحظات محفوظة حتى الآن.")
        st.stop()

    df = pd.DataFrame(records)
    total = len(df)
    alerts = df[df["early_warning"] == True]
    positive_count = int((df["sentiment"] == "إيجابي").sum())
    positive_rate = round((positive_count / total) * 100) if total else 0

    col1, col2, col3 = st.columns(3)
    col1.metric("التفاعلات المسجلة", total)
    col2.metric("الإنذارات المبكرة", len(alerts))
    col3.metric("نسبة الملاحظات الإيجابية", f"{positive_rate}%")

    st.divider()
    st.subheader("الإنذارات")
    if alerts.empty:
        st.success("لا توجد إنذارات مبكرة في السجلات الحالية.")
    else:
        for _, row in alerts.iterrows():
            with st.container(border=True):
                st.write(f"الموقع: {row['location_name']}")
                st.write(f"الإدارة: {row['department']}")
                st.write(
                    f"السبب: {row['root_cause']} "
                    f"— الخطورة {row['urgency_score']}/10"
                )
                st.write(f"الإجراء المقترح: {row['executive_action']}")

    st.divider()
    st.subheader("التوزيع الجغرافي")
    map_df = df[df["early_warning"] == True][
        ["latitude", "longitude"]
    ].dropna()

    if not map_df.empty:
        st.map(
            map_df,
            latitude="latitude",
            longitude="longitude",
            zoom=4,
        )
    else:
        st.info("ستظهر المواقع عند تسجيل إنذارات.")

    st.divider()
    st.subheader("سجل الملاحظات")
    visible_columns = [
        "created_at",
        "source_type",
        "location_name",
        "sentiment",
        "urgency_score",
        "root_cause",
        "department",
        "sanitized_text",
    ]
    st.dataframe(
        df[visible_columns],
        use_container_width=True,
        hide_index=True,
    )
