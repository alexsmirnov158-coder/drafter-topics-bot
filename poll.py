#!/usr/bin/env python3
"""
Master polling script for @Drafter_community content pipeline.

Phases (state.phase):
  idle               — between days, waiting for the daily 10:00 MSK tick
  waiting_topic      — topics sent today, waiting for user's choice in DM
  drafting           — topic chosen, will draft + cover + send preview
  awaiting_approval  — preview sent to DM, waiting for "публикуй"/"ок"
  publishing         — will post to @Drafter_community
  cooldown           — published, nothing to do until next morning

Triggers from envs (GitHub Actions secrets):
  BOT_TOKEN       Telegram bot token
  CHAT_ID         Owner chat id (DM)
  ANTHROPIC_KEY   Claude API key
  HF_TOKEN        HuggingFace token
  GH_PAT          Personal access token for pushing to drafter-covers
"""
import os, sys, json, random, base64, pathlib, urllib.request, urllib.error, subprocess
from datetime import datetime, timezone, timedelta

REPO = pathlib.Path(__file__).resolve().parent
STATE_PATH = REPO / "state.json"
BANK_PATH = REPO / "topics-bank.tsv"
COVERS_REPO_URL = "https://github.com/alexsmirnov158-coder/drafter-covers.git"
COVERS_RAW_BASE = "https://raw.githubusercontent.com/alexsmirnov158-coder/drafter-covers/main"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
HF_TOKEN = os.environ["HF_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
CHANNEL = "@Drafter_community"

# 10:00 Europe/Moscow == 07:00 UTC
TOPICS_HOUR_UTC = 7


# ------------------------------------------------------------------ helpers

def now_utc():
    return datetime.now(timezone.utc)


def load_state():
    return json.loads(STATE_PATH.read_text())


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def tg_request(method, **fields):
    body_parts, boundary = [], "----py" + os.urandom(8).hex()
    crlf = "\r\n"
    body = b""
    for name, value in fields.items():
        body += (
            f"--{boundary}{crlf}"
            f'Content-Disposition: form-data; name="{name}"{crlf}{crlf}{value}{crlf}'
        ).encode("utf-8")
    body += f"--{boundary}--{crlf}".encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_send_message(chat_id, text, parse_mode=None, link_preview_options=None):
    fields = {"chat_id": chat_id, "text": text}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    if link_preview_options is not None:
        fields["link_preview_options"] = json.dumps(link_preview_options)
    return tg_request("sendMessage", **fields)


def tg_get_updates(offset):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=0"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))["result"]


def call_claude(system_prompt, user_prompt, model="claude-sonnet-4-6"):
    body = json.dumps({
        "model": model,
        "max_tokens": 4000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return "".join(blk["text"] for blk in resp["content"] if blk["type"] == "text")


def call_huggingface_image(prompt, out_path):
    from huggingface_hub import InferenceClient
    from huggingface_hub.errors import HfHubHTTPError
    import time
    client = InferenceClient(api_key=HF_TOKEN)
    # Retry on rate-limit, then fallback to other models
    last_exc = None
    for model, delay in [
        ("black-forest-labs/FLUX.1-schnell", 0),
        ("black-forest-labs/FLUX.1-schnell", 30),
        ("black-forest-labs/FLUX.1-dev", 0),
        ("stabilityai/stable-diffusion-xl-base-1.0", 0),
    ]:
        if delay:
            time.sleep(delay)
        try:
            image = client.text_to_image(
                prompt=prompt, model=model, width=1280, height=720,
            )
            image.save(out_path)
            print(f"cover via {model}")
            return
        except HfHubHTTPError as e:
            last_exc = e
            msg = str(e)
            if "429" in msg or "503" in msg or "rate limit" in msg.lower():
                continue
            raise
    raise last_exc


def upload_cover_to_repo(local_path, dest_name):
    """Clone drafter-covers, copy file, commit, push, return public URL."""
    workdir = pathlib.Path("/tmp/drafter-covers-tmp")
    if workdir.exists():
        subprocess.run(["rm", "-rf", str(workdir)], check=True)
    auth_url = f"https://x-access-token:{GH_PAT}@github.com/alexsmirnov158-coder/drafter-covers.git"
    subprocess.run(["git", "clone", "-q", "--depth", "1", auth_url, str(workdir)], check=True)
    dest = workdir / dest_name
    subprocess.run(["cp", str(local_path), str(dest)], check=True)
    subprocess.run(["git", "-C", str(workdir), "config", "user.email", "bot@drafter.local"], check=True)
    subprocess.run(["git", "-C", str(workdir), "config", "user.name", "drafter-topics-bot"], check=True)
    subprocess.run(["git", "-C", str(workdir), "add", dest_name], check=True)
    subprocess.run(["git", "-C", str(workdir), "commit", "-q", "-m", f"Add {dest_name}"], check=True)
    subprocess.run(["git", "-C", str(workdir), "push", "-q"], check=True)
    return f"{COVERS_RAW_BASE}/{dest_name}"


# ------------------------------------------------------------------ phase handlers

def send_topics(state):
    rows = []
    with open(BANK_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            cat, _, title = line.partition("\t")
            if title:
                rows.append((cat.strip(), title.strip()))
    random.shuffle(rows)
    picked, seen = [], set()
    for cat, title in rows:
        if cat not in seen:
            picked.append((cat, title))
            seen.add(cat)
            if len(picked) == 5:
                break
    if len(picked) < 5:
        for cat, title in rows:
            if (cat, title) not in picked:
                picked.append((cat, title))
                if len(picked) == 5:
                    break
    lines = ["☀️ Темы на сегодня:", ""]
    for i, (cat, title) in enumerate(picked, 1):
        lines.append(f"{i}. {cat} — {title}")
    lines += ["", "Ответь номером или своей идеей."]
    text = "\n".join(lines)
    tg_send_message(CHAT_ID, text)
    # Save what was sent so we can match user reply
    (REPO / "last-topics.txt").write_text(text)
    state["last_topics_date"] = now_utc().date().isoformat()
    state["phase"] = "waiting_topic"
    state["selected_topic"] = ""
    state["draft_text"] = ""
    state["cover_url"] = ""


def parse_user_topic(text, topics_text):
    """Return selected topic string ('Category — Title') or None."""
    if not text:
        return None
    t = text.strip()
    # numeric choice
    if t.isdigit():
        n = int(t)
        for line in topics_text.splitlines():
            if line.startswith(f"{n}. "):
                return line[len(f"{n}. "):].strip()
        return None
    # if user pastes the full line
    for line in topics_text.splitlines():
        if line.startswith(tuple(f"{i}. " for i in range(1, 10))):
            stripped = line.split(". ", 1)[1].strip() if ". " in line else ""
            if stripped == t:
                return stripped
    # if user replies with title only (after dash)
    if " — " in t and len(t) > 10:
        return t
    return None


def draft_post(state):
    topic = state["selected_topic"]
    # ----- Claude: write the post
    system_prompt = (
        "Ты редактор Telegram-канала @Drafter_community для фаундеров и инвесторов в РФ. "
        "Пишешь в стиле Портнягина с expandable blockquote: жирный заголовок одной строкой, "
        "лид 1-2 предложения, контекст 1 абзац, явная польза для читателя одним абзацем, "
        "жирный sub-heading «X пунктов, …», нумерованный список (1. 2. 3.) ВНУТРИ "
        "<blockquote expandable>…</blockquote>, афоризм снаружи блока, CTA одним предложением "
        "(«Сохрани пост…», «Перешли тому, кто…»). "
        "Только HTML-теги <b>, <blockquote expandable>, <a href>. "
        "Длина видимого текста 1500–2500 символов. "
        "Без LLM-штампов («звучит гордо», «не X а Y»), без эмодзи внутри текста, без хэштегов, "
        "без ссылок на новости. Конкретные примеры с цифрами в формате «5 млн ₽», «3 месяца», "
        "названиями (Wildberries, РБК, ФРИИ и т.п.). На «ты»/«вы» — смесь, дружелюбно-экспертный тон. "
        "Возвращай ТОЛЬКО готовый HTML-текст поста, без преамбулы, без markdown-обёрток."
    )
    user_prompt = f"Тема: {topic}\n\nНапиши пост."
    raw = call_claude(system_prompt, user_prompt).strip()
    # strip accidental code fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()
    state["draft_text"] = raw

    # ----- HF: write the cover prompt
    scene_system = (
        "Generate ONE concise English Pollinations/Flux-style prompt for an editorial cover image "
        "based on the given Russian topic. Output ONLY the prompt, no preamble. Photorealistic, "
        "magazine cover, soft natural light, neutral palette, no people facing camera, no readable "
        "text, no logos, no branding."
    )
    scene_prompt = call_claude(scene_system, topic, model="claude-haiku-4-5-20251001").strip()
    out_path = pathlib.Path("/tmp/cover.png")
    call_huggingface_image(scene_prompt, out_path)

    # ----- upload to drafter-covers
    today = now_utc().date().isoformat()
    slug = "".join(c if c.isalnum() else "-" for c in topic.split("—")[-1].strip())[:50].strip("-").lower() or "post"
    dest_name = f"{today}-{slug}.png"
    cover_url = upload_cover_to_repo(out_path, dest_name)
    state["cover_url"] = cover_url

    # ----- send preview to DM
    lpo = {"is_disabled": False, "url": cover_url, "prefer_large_media": True, "show_above_text": True}
    resp = tg_send_message(CHAT_ID, raw, parse_mode="HTML", link_preview_options=lpo)
    if not resp.get("ok"):
        raise RuntimeError(f"Preview send failed: {resp}")
    # send a one-liner action prompt
    tg_send_message(CHAT_ID, "Ответь «публикуй» — отправлю в канал. Или «переделай» — сгенерирую заново.")
    state["phase"] = "awaiting_approval"


def publish_post(state):
    lpo = {"is_disabled": False, "url": state["cover_url"], "prefer_large_media": True, "show_above_text": True}
    resp = tg_send_message(CHANNEL, state["draft_text"], parse_mode="HTML", link_preview_options=lpo)
    if not resp.get("ok"):
        raise RuntimeError(f"Publish failed: {resp}")
    state["published_msg_id"] = resp["result"]["message_id"]
    tg_send_message(CHAT_ID, f"✅ Опубликовано: message_id {state['published_msg_id']}")
    state["phase"] = "cooldown"


# ------------------------------------------------------------------ main

def main():
    state = load_state()
    today = now_utc().date().isoformat()
    new_day = state.get("last_topics_date") != today

    # If a new day starts, reset cooldown so daily flow can fire again.
    if new_day and state["phase"] == "cooldown":
        state["phase"] = "idle"

    # 1. Daily topics tick
    if (
        state["phase"] == "idle"
        and now_utc().hour >= TOPICS_HOUR_UTC
        and state.get("last_topics_date") != today
    ):
        send_topics(state)

    # 2. Read user replies
    updates = tg_get_updates(state["last_update_id"] + 1)
    topics_text = ""
    try:
        topics_text = (REPO / "last-topics.txt").read_text()
    except FileNotFoundError:
        pass
    for u in updates:
        state["last_update_id"] = u["update_id"]
        msg = u.get("message")
        if not msg or str(msg["chat"]["id"]) != str(CHAT_ID):
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if state["phase"] == "waiting_topic":
            topic = parse_user_topic(text, topics_text)
            if topic:
                state["selected_topic"] = topic
                state["phase"] = "drafting"
                tg_send_message(CHAT_ID, f"🛠 Готовлю пост: {topic}\n\nОбычно занимает 1-2 минуты.")
        elif state["phase"] == "awaiting_approval":
            low = text.lower()
            if low in ("публикуй", "ок", "publish", "ok", "+", "да", "yes"):
                state["phase"] = "publishing"
            elif low in ("переделай", "regen", "переделать"):
                state["phase"] = "drafting"
                tg_send_message(CHAT_ID, "🛠 Переделываю…")

    # 3. Drafting
    if state["phase"] == "drafting":
        try:
            draft_post(state)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            tg_send_message(CHAT_ID, f"⚠️ Ошибка при подготовке поста: {type(e).__name__}: {e}\n\n```\n{tb[-2000:]}\n```")
            state["phase"] = "waiting_topic"

    # 4. Publishing
    if state["phase"] == "publishing":
        try:
            publish_post(state)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            tg_send_message(CHAT_ID, f"⚠️ Ошибка при публикации: {type(e).__name__}: {e}\n\n```\n{tb[-2000:]}\n```")
            state["phase"] = "awaiting_approval"

    save_state(state)
    print(f"phase={state['phase']} | last_update_id={state['last_update_id']}")


if __name__ == "__main__":
    main()
