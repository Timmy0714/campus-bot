# app.py

import os
import tempfile

from flask import Flask, request, abort
from dotenv import load_dotenv

load_dotenv()

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    AudioMessage,
    TextSendMessage,
)

from pydub import AudioSegment

from services.speech_translate_service import (
    speech_to_text_auto,
    translate_text,
)

app = Flask(__name__)

# ===== LINE 設定 =====
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("WARN: LINE credentials are missing. Set LINE_CHANNEL_SECRET & LINE_CHANNEL_ACCESS_TOKEN.")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN) if CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None

# ===== 簡單記憶體設定（不用 DB） =====
# user_settings = {
#   user_id: {
#       "translate_on": bool,
#       "target_lang": "...",
#       "awaiting_mode_choice": bool,
#       "service_mode": "none" / "voice" / "text",
#       "awaiting_text_lang_choice": bool,
#   }
# }
user_settings: dict[str, dict] = {}


def get_user_setting(user_id: str, key: str, default=None):
    return user_settings.get(user_id, {}).get(key, default)


def set_user_settings(user_id: str, **kwargs):
    settings = user_settings.setdefault(
        user_id,
        {
            "translate_on": False,
            "target_lang": "zh-Hant",
            "awaiting_mode_choice": False,
            "service_mode": "none",
            "awaiting_text_lang_choice": False,
        },
    )
    settings.update(kwargs)


def human_lang_label(lang_code: str) -> str:
    mapping = {
        "zh-Hant": "繁體中文 (zh)",
        "en": "英文 (en)",
        "ja": "日文 (ja)",
        "ko": "韓文 (ko)",
        "de": "德文 (de)",
        "es": "西班牙文 (es)",
        "hi": "印度文 (hi)",
    }
    return mapping.get(lang_code, lang_code)


def text_lang_menu() -> str:
    return (
        "請選擇翻譯目標語言：\n"
        "1. 繁體中文 (zh)\n"
        "2. 英文 (en)\n"
        "3. 日文 (ja)\n"
        "4. 韓文 (ko)\n"
        "5. 德文 (de)\n"
        "6. 西班牙文 (es)\n"
        "7. 印度文 (hi)\n"
        "請輸入 1–7 選擇語言。\n"
        "若想離開文字翻譯服務、回到主選單，請輸入 0。"
    )


# ===== 基本 Web 頁面 =====
@app.route("/")
def index():
    return "Campus Translation Bot is running. 用 /help 或 /translate 看指令。"


@app.route("/healthz")
def healthz():
    return "ok"


# ===== Line Webhook =====
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    if not handler:
        return "LINE handler is not configured.", 500

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ===== 處理文字訊息 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    user_id = getattr(event.source, "user_id", "DEMO_USER")
    text = (event.message.text or "").strip()

    # ---------- 語言選單狀態（數字 0–7） ----------
    if get_user_setting(user_id, "awaiting_text_lang_choice", False):
        if text == "0":
            # 離開文字翻譯服務，回到主選單
            set_user_settings(
                user_id,
                awaiting_text_lang_choice=False,
                service_mode="none",
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "已離開文字翻譯服務。\n"
                        "若要重新選擇服務，請輸入：/translate"
                    )
                ),
            )
            return

        num_to_lang = {
            "1": "zh-Hant",
            "2": "en",
            "3": "ja",
            "4": "ko",
            "5": "de",
            "6": "es",
            "7": "hi",
        }
        if text in num_to_lang:
            target_lang = num_to_lang[text]
            set_user_settings(
                user_id,
                awaiting_text_lang_choice=False,
                target_lang=target_lang,
                service_mode="text",
            )
            label = human_lang_label(target_lang)
            msg = (
                f"✅ 翻譯目標語言已設定為：{label}\n\n"
                "現在你可以直接輸入任何文字，我會自動幫你翻譯。\n"
                "若之後想再更改語言，可以輸入：/lang\n"
                "若想回到主選單，請輸入：/translate"
            )
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=msg)
            )
            return

        # 輸入不是 0–7
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請輸入 1–7 選擇語言，或輸入 0 回到主選單。"),
        )
        return

    # ---------- 服務模式選單（1.語音 2.文字） ----------
    if get_user_setting(user_id, "awaiting_mode_choice", False):
        if text == "1":
            set_user_settings(
                user_id,
                awaiting_mode_choice=False,
                service_mode="voice",
                translate_on=True,
            )
            target_lang = get_user_setting(user_id, "target_lang", "zh-Hant")
            label = human_lang_label(target_lang)
            msg = (
                "✅ 已啟動「語音翻譯服務」。\n"
                "之後只要傳語音訊息，我會自動幫你辨識並翻譯。\n\n"
                f"目前翻譯目標語言：{label}\n"
                "若要更改目標語言，可以先切換到文字翻譯服務，或輸入 /translate lang <zh|en|ja|ko|de|es|hi>。\n"
                "若想回到主選單，請再輸入：/translate"
            )
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=msg)
            )
            return
        elif text == "2":
            # 進入文字翻譯服務 → 先選語言
            set_user_settings(
                user_id,
                awaiting_mode_choice=False,
                service_mode="text",
                awaiting_text_lang_choice=True,
            )
            msg = (
                "✅ 已啟動「文字翻譯服務」。\n"
                "請先選擇翻譯目標語言：\n\n"
                + text_lang_menu()
            )
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=msg)
            )
            return
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請輸入 1（語音翻譯）或 2（文字翻譯）喔～"),
            )
            return

    # ---------- /help ----------
    if text.startswith("/help"):
        reply = (
            "翻譯 Bot 指令：\n"
            "/translate            顯示翻譯服務選單\n"
            "/translate on         開啟語音翻譯\n"
            "/translate off        關閉語音翻譯\n"
            "/translate lang <zh|en|ja|ko|de|es|hi>  直接用代碼改目標語言\n"
            "/lang                 在文字翻譯服務中再次選擇語言（1–7）\n"
            "/tr <文字>            （選用）手動文字翻譯\n\n"
            "★ 若啟動「文字翻譯服務」，直接輸入文字就會自動翻譯。\n"
            "★ 若啟動「語音翻譯服務」，傳語音訊息會自動辨識並翻譯。"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply),
        )
        return

    # ---------- /translate 系列 ----------
    if text.startswith("/translate"):
        tokens = text.split()

        # /translate → 顯示服務選單
        if len(tokens) == 1:
            set_user_settings(
                user_id,
                awaiting_mode_choice=True,
                awaiting_text_lang_choice=False,
            )
            msg = (
                "請選擇翻譯服務：\n"
                "1. 語音翻譯服務（傳語音我幫你翻）\n"
                "2. 文字翻譯服務（直接打字我幫你翻）\n"
                "（請輸入 1 或 2）"
            )
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=msg)
            )
            return

        # /translate on/off
        if len(tokens) == 2 and tokens[1] in ("on", "off"):
            on = tokens[1] == "on"
            set_user_settings(user_id, translate_on=on)
            msg = "語音翻譯已開啟 ✅" if on else "語音翻譯已關閉 ❌"
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=msg)
            )
            return

        # /translate lang <code>（進階用，保留）
        if len(tokens) == 3 and tokens[1] == "lang":
            code = tokens[2].lower()
            lang_map = {
                "zh": "zh-Hant",
                "en": "en",
                "ja": "ja",
                "ko": "ko",
                "de": "de",
                "es": "es",
                "hi": "hi",
            }
            if code not in lang_map:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="目前支援語言代碼：zh / en / ja / ko / de / es / hi"
                    ),
                )
                return

            target_lang = lang_map[code]
            set_user_settings(user_id, target_lang=target_lang)
            label = human_lang_label(target_lang)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"翻譯目標語言已設定為：{label}\n（也可以在文字翻譯服務中輸入 /lang 用 1–7 重新選擇）"
                ),
            )
            return

        # 其他 /translate 用法錯誤
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="用法：\n/translate\n/translate on|off\n/translate lang <zh|en|ja|ko|de|es|hi>"
            ),
        )
        return

    # ---------- /lang：在文字翻譯服務中重新選語言 ----------
    if text.startswith("/lang"):
        if get_user_setting(user_id, "service_mode", "none") != "text":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="請先啟動文字翻譯服務：輸入 /translate 然後選 2。"
                ),
            )
            return

        set_user_settings(user_id, awaiting_text_lang_choice=True)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=text_lang_menu()),
        )
        return

    # ---------- /tr：保留手動文字翻譯 ----------
    if text.startswith("/tr "):
        src = text[len("/tr "):].strip()
        if not src:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請在 /tr 後面接要翻譯的文字。"),
            )
            return

        target_lang = get_user_setting(user_id, "target_lang", "zh-Hant")
        result = translate_text(src, to_lang=target_lang)
        if not result:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="翻譯失敗，請稍後再試 QQ"),
            )
            return

        label = human_lang_label(target_lang)
        msg = (
            f"目前翻譯目標語言：{label}\n"
            "（可用 /lang 或 /translate lang <code> 更改）\n\n"
            f"原文：\n{src}\n\n"
            f"翻譯：\n{result}"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=msg),
        )
        return

    # ---------- 自動文字翻譯模式 ----------
    service_mode = get_user_setting(user_id, "service_mode", "none")
    if service_mode == "text" and not text.startswith("/"):
        target_lang = get_user_setting(user_id, "target_lang", "zh-Hant")
        result = translate_text(text, to_lang=target_lang)
        if not result:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="翻譯失敗，請稍後再試 QQ"),
            )
            return

        label = human_lang_label(target_lang)
        msg = (
            f"目前翻譯目標語言：{label}\n"
            "（可輸入 /lang 用 1–7 更改，或 /translate 回到主選單）\n\n"
            f"原文：\n{text}\n\n"
            f"翻譯：\n{result}"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=msg),
        )
        return

    # ---------- 其他文字：提示 ----------
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=(
                "嗨～這裡是AI校園助理。\n"
                "你可以輸入 /translate 開啟語言和語音服務選單，"
                "或輸入 /help 查看所有指令。"
            )
        ),
    )


# ===== 處理語音訊息 =====
@handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event: MessageEvent):
    user_id = getattr(event.source, "user_id", "DEMO_USER")

    translate_on = get_user_setting(user_id, "translate_on", False)
    target_lang = get_user_setting(user_id, "target_lang", "zh-Hant")

    if not translate_on:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="語音翻譯尚未開啟，請先輸入：/translate 或 /translate on"
            ),
        )
        return

    # 1. 從 LINE 抓音訊 (m4a)
    message_content = line_bot_api.get_message_content(event.message.id)

    # 2. 存成 m4a 暫存檔
    with tempfile.NamedTemporaryFile(delete=False, suffix=".m4a") as tf:
        for chunk in message_content.iter_content():
            tf.write(chunk)
        m4a_path = tf.name
    print("[handle_audio_message] Saved m4a to:", m4a_path)

    # 3. 轉成 wav
    wav_path = m4a_path + ".wav"
    try:
        audio = AudioSegment.from_file(m4a_path)
        audio.export(wav_path, format="wav")
        print("[handle_audio_message] Converted wav to:", wav_path)
    except Exception as e:
        print("[handle_audio_message] convert m4a -> wav failed:", e)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="語音格式轉換失敗，請再試一次看看 QQ"),
        )
        return

    # 4. 自動偵測語言 + 辨識（支援中 / 英 / 日 / 韓 / 德 / 西 / 印地文）
    transcript, detected_lang = speech_to_text_auto(
        wav_path,
        possible_languages=[
            "en-US",  # 英文
            "zh-TW",  # 繁體中文
            "ja-JP",  # 日文
            "ko-KR",  # 韓文
        ],
    )

    if not transcript:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="語音辨識失敗，請再試一次。"),
        )
        return

    # 5. 翻譯成使用者設定的目標語言
    translated = translate_text(transcript, to_lang=target_lang) or "(翻譯失敗)"
    label = human_lang_label(target_lang)

    msg = (
        f"🔎 偵測語言：{detected_lang or '未知'}\n"
        f"目前翻譯目標語言：{label}\n"
        "（可輸入 /translate 或 /lang 更改設定）\n\n"
        f"🎙️ 語音辨識結果：\n{transcript}\n\n"
        f"🌐 翻譯：\n{translated}"
    )
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=msg),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
