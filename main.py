import os
import json
import time
import random
import asyncio
import logging
from pathlib import Path

import edge_tts
from google import genai
from google.genai import types
from moviepy import VideoFileClip, AudioFileClip, CompositeAudioClip
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ForceReply,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8618881977:AAHHOCc_H9CdzVCEiFny4wKeq57XlqRgIqs"
GEMINI_MODEL = "gemini-3.6-flash"

MAX_RETRIES = 4
UPLOAD_TIMEOUT = 60
FILE_PROCESS_TIMEOUT = 180
MODEL_TIMEOUT = 180
POLL_INTERVAL = 3

VOICE_MALE = "my-MM-ThihaNeural"
VOICE_FEMALE = "my-MM-NilarNeural"

INACTIVITY_LIMIT = 3600

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Dynamic Client Helper
# -----------------------------------------------------------------------------
def get_user_gemini_client(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    api_key = context.user_data.get("gemini_api_key")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


# -----------------------------------------------------------------------------
# Telegram Keyboards
# -----------------------------------------------------------------------------
def get_main_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔑 Add Gemini API", "❌ Remove Gemini API"]
        ],
        resize_keyboard=True,
        input_field_placeholder="အောက်ပါ မီနူးများကို အသုံးပြုပါ..."
    )

def get_voice_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Thiha (အမျိုးသား)", callback_data="voice_male"),
            InlineKeyboardButton("Nilar (အမျိုးသမီး)", callback_data="voice_female"),
        ]
    ])

def get_ratio_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 9:16 (TikTok/Reels)", callback_data="ratio_9_16"),
            InlineKeyboardButton("📸 4:5 (Instagram)", callback_data="ratio_4_5"),
        ],
        [
            InlineKeyboardButton("➡️ မူရင်းအတိုင်း (Original)", callback_data="ratio_original"),
        ]
    ])

def get_speed_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1.0x (ပုံမှန်)", callback_data="speed_1.0"),
            InlineKeyboardButton("1.2x (သင့်တော်)", callback_data="speed_1.2"),
            InlineKeyboardButton("1.3x", callback_data="speed_1.3"),
        ],
        [
            InlineKeyboardButton("1.4x", callback_data="speed_1.4"),
            InlineKeyboardButton("1.5x", callback_data="speed_1.5"),
            InlineKeyboardButton("1.6x (မြန်)", callback_data="speed_1.6"),
        ]
    ])

def get_clear_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ ဆာဗာဖိုင်များကို ရှင်းလင်းရန် (Clear)", callback_data="clear_server_files")]
    ])


# -----------------------------------------------------------------------------
# Video Crop Helper for 9:16 or 4:5
# -----------------------------------------------------------------------------
def adjust_video_aspect_ratio(clip: VideoFileClip, target_ratio: str) -> VideoFileClip:
    if target_ratio == "original":
        return clip

    W, H = clip.size
    
    if target_ratio == "9_16":
        target_w = H * 9 // 16
        target_h = H
        if target_w > W:
            target_w = W
            target_h = W * 16 // 9
    elif target_ratio == "4_5":
        target_w = H * 4 // 5
        target_h = H
        if target_w > W:
            target_w = W
            target_h = W * 5 // 4
    else:
        return clip

    x1 = (W - target_w) // 2
    y1 = (H - target_h) // 2
    x2 = x1 + target_w
    y2 = y1 + target_h

    try:
        from moviepy.video.fx.Crop import crop
        return crop(clip, x1=x1, y1=y1, x2=x2, y2=y2)
    except Exception:
        try:
            return clip.cropped(x1=x1, y1=y1, x2=x2, y2=y2)
        except Exception:
            return clip


# -----------------------------------------------------------------------------
# ASS Subtitle Generator for Myanmar Subtitles (FFmpeg Burn-in)
# -----------------------------------------------------------------------------
def seconds_to_ass_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centisecs = int(round((seconds - int(seconds)) * 100))
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

def generate_ass_subtitle_file(scenes: list, ass_filename: str):
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Myanmar,55,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,2,2,40,40,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for scene in scenes:
        start_str = seconds_to_ass_time(scene["start"])
        end_str = seconds_to_ass_time(scene["end"])
        text = scene["text"].replace("\n", " ")
        events.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}")

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(events))


# -----------------------------------------------------------------------------
# Robust Gemini helpers
# -----------------------------------------------------------------------------
def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    message = str(exc).lower()
    retryable_words = (
        "500", "502", "503", "504",
        "internal server error", "service unavailable",
        "deadline exceeded", "timeout", "timed out",
        "temporarily unavailable", "connection reset",
    )
    return any(word in message for word in retryable_words)


async def wait_before_retry(attempt: int):
    delay = min(3 * (2 ** attempt), 30) + random.uniform(0, 1.5)
    logger.warning("Waiting %.1f seconds before retry", delay)
    await asyncio.sleep(delay)


def validate_scenes(scenes, total_duration: float):
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Gemini response is not a non-empty JSON array")

    result = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"Scene {index} is not an object")

        try:
            start = float(scene["start"])
            end = float(scene["end"])
            raw_text = str(scene["text"]).strip()
            text = raw_text.replace(" ", "")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid fields in scene {index}") from exc

        if not text:
            continue

        result.append({
            "start": max(0.0, start),
            "end": min(total_duration, end),
            "text": text,
        })

    result.sort(key=lambda item: item["start"])
    return result


async def ensure_full_coverage(scenes, total_duration: float):
    if not isinstance(scenes, list) or not scenes:
        return [{"start": 0.0, "end": total_duration, "text": "ကဲဒီဗီဒီယိုလေးထဲမှာဘာတွေဆက်ဖြစ်မလဲကြည့်ရအောင်"}]

    scenes = sorted(scenes, key=lambda item: item["start"])
    scenes[0]["start"] = 0.0

    for i in range(len(scenes) - 1):
        current_end = float(scenes[i]["end"])
        next_start = float(scenes[i + 1]["start"])
        if current_end != next_start:
            scenes[i]["end"] = next_start

    scenes[-1]["end"] = total_duration

    valid_scenes = []
    for scene in scenes:
        text = str(scene.get("text", "")).strip().replace(" ", "")
        start = float(scene.get("start", 0.0))
        end = float(scene.get("end", total_duration))
        
        if start < end:
            valid_scenes.append({
                "start": max(0.0, start),
                "end": min(total_duration, end),
                "text": text if text else "..."
            })

    return valid_scenes


async def generate_scenes_with_verification(client, file_path: str, total_duration: float):
    prompt = (
        f"You are an expert Burmese Movie Recapper and Continuous Storyteller. "
        f"The total video duration is exactly {total_duration:.1f} seconds.\n\n"
        "STRICT NARRATION RULES (CRITICAL):\n"
        "1. VIDEO INTRO: Scene 0 MUST start with a catchy introductory phrase (e.g., 'ကဲဒီဗီဒီယိုလေးထဲမှာဘာတွေဆက်ဖြစ်မလဲအတူတူကြည့်ကြရအောင်').\n"
        "2. ENGAGING RECAP STYLE: Make the transcript super engaging and dramatic.\n"
        "3. CONTINUOUS FLOW: Flow like a continuous movie recap.\n"
        "4. SCENE SYNCHRONIZATION: Describe what is happening visually during each timeframe.\n"
        "5. BURMESE NARRATION STYLE: Use natural connectors like 'ဒီလိုနဲ့', 'အဲဒီအချိန်မှာ', 'ရုတ်တရက်'.\n"
        "6. NO SPACES IN BURMESE: DO NOT put spaces between Burmese words!\n\n"
        "STRICT TIMELINE RULES:\n"
        "• Scene 0 MUST start at 0.0.\n"
        "• ZERO GAP RULE: Scene N 'end' MUST exactly equal Scene N+1 'start'.\n"
        "• The very last Scene's 'end' MUST be exactly {total_duration:.1f}.\n\n"
        "OUTPUT FORMAT:\n"
        "• Return ONLY a valid JSON array: [{\"start\": float, \"end\": float, \"text\": string}]\n"
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        uploaded_file = None
        started_at = time.monotonic()

        try:
            uploaded_file = await asyncio.wait_for(
                asyncio.to_thread(client.files.upload, file=file_path),
                timeout=UPLOAD_TIMEOUT,
            )

            while True:
                if time.monotonic() - started_at > FILE_PROCESS_TIMEOUT:
                    raise TimeoutError("Gemini video processing timeout")

                state_name = getattr(getattr(uploaded_file, "state", None), "name", "")
                if state_name in ("ACTIVE", "READY"):
                    break
                if state_name == "FAILED":
                    raise RuntimeError("Gemini failed to process uploaded video")

                await asyncio.sleep(POLL_INTERVAL)
                uploaded_file = await asyncio.wait_for(
                    asyncio.to_thread(lambda: client.files.get(name=uploaded_file.name)),
                    timeout=30,
                )

            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=[uploaded_file, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.3,
                        )
                    )
                ),
                timeout=MODEL_TIMEOUT,
            )

            raw_response = (response.text or "").strip()
            if not raw_response:
                raise ValueError("Gemini returned empty response")

            if raw_response.startswith("```"):
                raw_response = raw_response.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

            scenes = validate_scenes(json.loads(raw_response), total_duration)
            return await ensure_full_coverage(scenes, total_duration)

        except Exception as exc:
            last_error = exc
            err_str = str(exc).lower()
            if "429" in err_str or "rate limit" in err_str or "resource exhausted" in err_str:
                raise RuntimeError("API_LIMIT_EXCEEDED")

            if not is_retryable_error(exc):
                raise RuntimeError(f"Non-retryable Gemini error: {exc}") from exc
            if attempt < MAX_RETRIES - 1:
                await wait_before_retry(attempt)
        finally:
            if uploaded_file is not None:
                try:
                    await asyncio.to_thread(lambda: client.files.delete(name=uploaded_file.name))
                except Exception:
                    pass

    raise RuntimeError(f"Gemini failed after {MAX_RETRIES} attempts: {last_error}")


def format_timestamp(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


# -----------------------------------------------------------------------------
# Video Export Helper with Subtitles Burn-in via FFmpeg
# -----------------------------------------------------------------------------
def render_video_with_subtitles(video_clip, final_audio, scenes, output_path, temp_audio_file):
    ass_filename = f"subtitles_{random.randint(1000, 9999)}.ass"
    generate_ass_subtitle_file(scenes, ass_filename)

    try:
        try:
            final_clip = video_clip.with_audio(final_audio)
        except AttributeError:
            final_clip = video_clip.set_audio(final_audio)

        # FFmpeg filter ဖြင့် Noto Sans Myanmar ဖောင့်သုံးကာ စာတန်းထိုးများကို Burn-in လုပ်ခြင်း
        # Linux VPS တွင် fontconfig က font.ttf ကို သိစေရန် fontsdir ကို ညွှန်းပေးရပါမည်
        current_dir = os.path.abspath(os.getcwd())
        ass_filter = f"ass={ass_filename}:fontsdir={current_dir}"

        final_clip.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            logger=None,
            temp_audiofile=temp_audio_file,
            remove_temp=True,
            ffmpeg_params=["-vf", ass_filter]
        )
        final_clip.close()
    finally:
        if os.path.exists(ass_filename):
            try:
                os.remove(ass_filename)
            except:
                pass


# -----------------------------------------------------------------------------
# Preliminary Preview Generator
# -----------------------------------------------------------------------------
async def generate_preview_and_transcripts(update_or_query, context, file_path, selected_voice, speed_multiplier, aspect_ratio, is_callback=True):
    chat_id = update_or_query.message.chat_id if is_callback else update_or_query.chat_id
    target_message = update_or_query.message if is_callback else update_or_query

    client = get_user_gemini_client(context, chat_id)
    if not client:
        await target_message.reply_text("❌ ကျေးဇူးပြု၍ '🔑 Add Gemini API' ကိုနှိပ်ပြီး သင်၏ Gemini API Key ကို အရင်ထည့်ပါ။", reply_markup=get_main_reply_keyboard())
        return

    temp_audio_file = f"temp-audio-{chat_id}.m4a"
    created_temp_audios = []
    raw_clips = []
    raw_video_clip = None
    video_clip = None
    audio_clips = []
    preview_video_path = f"preview_{chat_id}.mp4"

    try:
        probe = VideoFileClip(file_path)
        total_duration = float(probe.duration)
        probe.close()

        status_msg = await target_message.reply_text("✍️ Gemini မှ Video ကိုလေ့လာပြီး မြန်မာဇာတ်ညွှန်း ရေးဖွဲ့နေပါသည်...")

        try:
            scenes = await generate_scenes_with_verification(client, file_path, total_duration)
        except RuntimeError as re_err:
            if "API_LIMIT_EXCEEDED" in str(re_err):
                await status_msg.edit_text(
                    "⚠️ **Gemini API Limit ပြည့်သွားပါပြီ!** ကျေးဇူးပြု၍ API အသစ်လဲလှယ်ပေးပါ။",
                    reply_markup=get_main_reply_keyboard()
                )
                return
            else:
                raise re_err

        if not scenes:
            raise RuntimeError("Gemini returned no scenes")

        await status_msg.edit_text("🎙️ အသံဖိုင်များ ဖန်တီးနေပါသည်...")
        
        raw_video_clip = VideoFileClip(file_path)
        video_clip = adjust_video_aspect_ratio(raw_video_clip, aspect_ratio)

        current_time = 0.0
        updated_scenes = []

        for index, scene in enumerate(scenes):
            text = scene["text"].strip()
            if not text:
                continue

            temp_audio_path = f"scene_{chat_id}_{index}.mp3"
            created_temp_audios.append(temp_audio_path)
            
            percent = int(round((speed_multiplier - 1.0) * 100))
            rate_str = f"+{percent}%" if percent >= 0 else f"{percent}%"
            await edge_tts.Communicate(text, selected_voice, rate=rate_str).save(temp_audio_path)

            raw_audio = AudioFileClip(temp_audio_path)
            raw_clips.append(raw_audio)

            start_at = current_time
            audio_duration = float(raw_audio.duration)
            end_at = start_at + audio_duration

            try:
                positioned_audio = raw_audio.with_start(start_at)
            except AttributeError:
                positioned_audio = raw_audio.set_start(start_at)

            audio_clips.append(positioned_audio)
            
            if index < len(scenes) - 1:
                original_next_start = float(scenes[index + 1]["start"])
                if end_at < original_next_start:
                    current_time = end_at + 1.0
                else:
                    current_time = end_at
            else:
                current_time = end_at

            updated_scenes.append({
                "start": start_at,
                "end": end_at,
                "text": text
            })

        scenes = updated_scenes
        final_audio = CompositeAudioClip(audio_clips)

        await status_msg.edit_text("🔤 မြန်မာစာတန်းများနှင့် ဗီဒီယို ပေါင်းစပ်နေပါပြီ (Burn-in Subtitles)...")
        render_video_with_subtitles(video_clip, final_audio, scenes, preview_video_path, temp_audio_file)

        await status_msg.delete()

        context.user_data["preview_video_path"] = preview_video_path
        context.user_data["scenes"] = scenes
        context.user_data["selected_voice"] = selected_voice
        context.user_data["speed_multiplier"] = speed_multiplier
        context.user_data["aspect_ratio"] = aspect_ratio
        context.user_data["total_duration"] = total_duration
        context.user_data["last_activity"] = time.time()

        preview_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ဇာတ်ညွှန်းများ ပြင်ဆင်ရန် (Edit)", callback_data="open_transcript_edit")],
            [InlineKeyboardButton("⚡ အမြန်နှုန်း ပြောင်းရန် (Change Speed)", callback_data="open_speed_change")]
        ])

        with open(preview_video_path, "rb") as vid_file:
            sent_vid_msg = await target_message.reply_video(
                video=vid_file,
                caption="🎥 **စစ်ဆေးရန် ဗီဒီယို (Preview Video with Burmese Subtitles)**\nအသံနှင့် မြန်မာစာတန်းထိုး ကိုက်ညီမှုရှိမရှိ စစ်ဆေးပါ။",
                supports_streaming=True,
                reply_markup=preview_markup
            )
            context.user_data["sent_video_msg_id"] = sent_vid_msg.message_id

    except Exception as exc:
        logger.exception("Error generating preview")
        await target_message.reply_text(f"❌ အမှားအယွင်း ဖြစ်ပေါ်သွားပါသည်: {exc}", reply_markup=get_main_reply_keyboard())
    finally:
        for clip in audio_clips + raw_clips:
            try: clip.close()
            except: pass
        if video_clip:
            try: video_clip.close()
            except: pass
        if raw_video_clip:
            try: raw_video_clip.close()
            except: pass


# -----------------------------------------------------------------------------
# Quick Speed Update Helper
# -----------------------------------------------------------------------------
async def render_with_current_scenes_and_speed(update, context, speed_multiplier):
    query = update.callback_query
    chat_id = query.message.chat_id
    target_message = query.message

    file_path = context.user_data.get("pending_video_path")
    scenes = context.user_data.get("scenes")
    selected_voice = context.user_data.get("selected_voice", VOICE_MALE)
    aspect_ratio = context.user_data.get("aspect_ratio", "original")

    if not file_path or not Path(file_path).exists() or not scenes:
        await query.edit_message_text("⚠️ ဒေတာများ မရှိတော့ပါ။ ကျေးဇူးပြု၍ ဗီဒီယိုအသစ် ပြန်ပို့ပါ။")
        return

    status_msg = await target_message.reply_text(f"⚡ အမြန်နှုန်း ({speed_multiplier}x) ဖြင့် ဗီဒီယိုကို ပြန်လည် တည်ဆောက်နေပါပြီ...")

    output_video_path = f"speed_updated_{chat_id}.mp4"
    temp_audio_file = f"temp-audio-speed-{chat_id}.m4a"
    created_temp_audios = []
    raw_clips = []
    raw_video_clip = None
    video_clip = None
    audio_clips = []

    try:
        raw_video_clip = VideoFileClip(file_path)
        video_clip = adjust_video_aspect_ratio(raw_video_clip, aspect_ratio)

        current_time = 0.0
        recalculated_scenes = []

        for index, scene in enumerate(scenes):
            text = scene["text"].strip()
            if not text:
                continue

            temp_audio_path = f"speed_scene_{chat_id}_{index}.mp3"
            created_temp_audios.append(temp_audio_path)
            
            percent = int(round((speed_multiplier - 1.0) * 100))
            rate_str = f"+{percent}%" if percent >= 0 else f"{percent}%"
            await edge_tts.Communicate(text, selected_voice, rate=rate_str).save(temp_audio_path)

            raw_audio = AudioFileClip(temp_audio_path)
            raw_clips.append(raw_audio)

            start_at = current_time
            audio_duration = float(raw_audio.duration)
            end_at = start_at + audio_duration

            try:
                positioned_audio = raw_audio.with_start(start_at)
            except AttributeError:
                positioned_audio = raw_audio.set_start(start_at)

            audio_clips.append(positioned_audio)
            current_time = end_at

            recalculated_scenes.append({
                "start": start_at,
                "end": end_at,
                "text": text
            })

        context.user_data["scenes"] = recalculated_scenes
        final_audio = CompositeAudioClip(audio_clips)

        render_video_with_subtitles(video_clip, final_audio, recalculated_scenes, output_video_path, temp_audio_file)

        preview_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ဇာတ်ညွှန်းများ ပြင်ဆင်ရန် (Edit)", callback_data="open_transcript_edit")],
            [InlineKeyboardButton("⚡ အမြန်နှုန်း ပြောင်းရန် (Change Speed)", callback_data="open_speed_change")]
        ])

        with open(output_video_path, "rb") as vid_file:
            sent_vid_msg = await context.bot.send_video(
                chat_id=chat_id,
                video=vid_file,
                caption=f"🎥 **အမြန်နှုန်းပြောင်းလဲထားသော ဗီဒီယို (Speed: {speed_multiplier}x)**",
                supports_streaming=True,
                reply_markup=preview_markup
            )
            context.user_data["sent_video_msg_id"] = sent_vid_msg.message_id

        await status_msg.delete()
        context.user_data["last_activity"] = time.time()

    except Exception as exc:
        logger.exception("Error updating speed")
        await target_message.reply_text(f"❌ အမြန်နှုန်းပြောင်းလဲရာတွင် အမှားအယွင်း ဖြစ်ပေါ်ပါသည်: {exc}")
    finally:
        for clip in audio_clips + raw_clips:
            try: clip.close()
            except: pass
        if video_clip:
            try: video_clip.close()
            except: pass
        if raw_video_clip:
            try: raw_video_clip.close()
            except: pass
        for path in created_temp_audios:
            try:
                if path and Path(path).exists():
                    Path(path).unlink()
            except:
                pass


# -----------------------------------------------------------------------------
# Final Rendering Process
# -----------------------------------------------------------------------------
async def finalize_and_render_video(update, context):
    query = update.callback_query
    chat_id = query.message.chat_id
    target_message = query.message

    file_path = context.user_data.get("pending_video_path")
    preview_video_path = context.user_data.get("preview_video_path")
    scenes = context.user_data.get("scenes")
    selected_voice = context.user_data.get("selected_voice")
    speed_multiplier = context.user_data.get("speed_multiplier", 1.2)
    aspect_ratio = context.user_data.get("aspect_ratio", "original")
    total_duration = context.user_data.get("total_duration", 0.0)

    if not scenes or not file_path or not Path(file_path).exists():
        await query.edit_message_text("⚠️ ဒေတာများ မရှိတော့ပါ။ ကျေးဇူးပြု၍ ဗီဒီယိုအသစ် ပြန်ပို့ပါ။")
        return

    status_msg = await target_message.reply_text("🔄 ပြင်ဆင်ထားသော ဇာတ်ညွှန်းများဖြင့် နောက်ဆုံးအကြိမ် ဗီဒီယို တည်ဆောက်နေပါပြီ...")

    output_video_path = f"final_{chat_id}.mp4"
    temp_audio_file = f"temp-audio-final-{chat_id}.m4a"
    created_temp_audios = []
    raw_clips = []
    raw_video_clip = None
    video_clip = None
    audio_clips = []

    try:
        raw_video_clip = VideoFileClip(file_path)
        video_clip = adjust_video_aspect_ratio(raw_video_clip, aspect_ratio)

        current_time = 0.0
        recalculated_scenes = []

        for index, scene in enumerate(scenes):
            text = scene["text"].strip()
            if not text:
                continue

            temp_audio_path = f"final_scene_{chat_id}_{index}.mp3"
            created_temp_audios.append(temp_audio_path)
            
            percent = int(round((speed_multiplier - 1.0) * 100))
            rate_str = f"+{percent}%" if percent >= 0 else f"{percent}%"
            await edge_tts.Communicate(text, selected_voice, rate=rate_str).save(temp_audio_path)

            raw_audio = AudioFileClip(temp_audio_path)
            raw_clips.append(raw_audio)

            start_at = current_time
            audio_duration = float(raw_audio.duration)
            end_at = start_at + audio_duration

            try:
                positioned_audio = raw_audio.with_start(start_at)
            except AttributeError:
                positioned_audio = raw_audio.set_start(start_at)

            audio_clips.append(positioned_audio)
            current_time = end_at

            recalculated_scenes.append({
                "start": start_at,
                "end": end_at,
                "text": text
            })

        scenes = recalculated_scenes
        final_audio = CompositeAudioClip(audio_clips)

        render_video_with_subtitles(video_clip, final_audio, scenes, output_video_path, temp_audio_file)

        await status_msg.edit_text("📤 အပြီးသတ် Video ကို ပို့နေပါသည်...")

        sent_vid_id = context.user_data.get("sent_video_msg_id")
        transcript_ids = context.user_data.get("sent_transcript_msg_ids", [])

        ids_to_delete = [sent_vid_id] + transcript_ids
        for msg_id in ids_to_delete:
            if msg_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass

        if preview_video_path and Path(preview_video_path).exists():
            try: Path(preview_video_path).unlink()
            except: pass

        context.user_data["final_video_path"] = output_video_path

        voice_label = "Thiha (အမျိုးသား)" if selected_voice == VOICE_MALE else "Nilar (အမျိုးသမီး)"
        ratio_mapping = {"9_16": "9:16 (Vertical)", "4_5": "4:5 (Portrait)", "original": "Original"}
        ratio_label = ratio_mapping.get(aspect_ratio, "Original")
        
        final_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ဇာတ်ညွှန်းများ ပြင်ဆင်ရန် (Edit)", callback_data="open_transcript_edit")],
            [InlineKeyboardButton("⚡ အမြန်နှုန်း ပြောင်းရန် (Change Speed)", callback_data="open_speed_change")],
            [InlineKeyboardButton("✅ ကျေနပ်ပါသည် (Done)", callback_data="user_satisfied")]
        ])

        with open(output_video_path, "rb") as video_file:
            sent_final_msg = await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=(
                    f"✅ **မြန်မာစာတန်းထိုး ဗီဒီယို အောင်မြင်စွာ ပြီးဆုံးပါပြီ။**\n\n"
                    f"📐 Size: {ratio_label} | ⚡ Speed: {speed_multiplier}x\n"
                    f"⏱ အရှည်: {total_duration:.1f} စက္ကန့် | 🎙️ အသံ: {voice_label}"
                ),
                supports_streaming=True,
                reply_markup=final_markup
            )
            context.user_data["sent_final_video_msg_id"] = sent_final_msg.message_id

        await status_msg.delete()
        context.user_data["last_activity"] = time.time()

    except Exception as exc:
        logger.exception("Error in final rendering")
        await target_message.reply_text(f"❌ Rendering အမှားအယွင်း: {exc}", reply_markup=get_main_reply_keyboard())
    finally:
        for clip in audio_clips + raw_clips:
            try: clip.close()
            except: pass
        if video_clip:
            try: video_clip.close()
            except: pass
        if raw_video_clip:
            try: raw_video_clip.close()
            except: pass
        for path in created_temp_audios:
            try:
                if path and Path(path).exists():
                    Path(path).unlink()
            except:
                pass


# -----------------------------------------------------------------------------
# Telegram Handlers
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_activity"] = time.time()
    await update.message.reply_text(
        "မင်္ဂလာပါ။ ဗီဒီယိုများတွင် မြန်မာအသံနှင့် စာတန်းထိုးပေးသော Bot မှ ကြိုဆိုပါတယ်။\n\n"
        "ပထမဦးစွာ သင်၏ Gemini API Key ကို ထည့်သွင်းရန် အောက်ပါ ခလုတ်ကို နှိပ်ပါ။",
        reply_markup=get_main_reply_keyboard()
    )


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_activity"] = time.time()
    text = update.message.text

    if text == "🔑 Add Gemini API":
        if context.user_data.get("gemini_api_key"):
            await update.message.reply_text(
                "⚠️ သင့်တွင် Gemini API Key တစ်ခု ရှိပြီးသားဖြစ်ပါသည်။ API အသစ်ထည့်လိုလျှင် အရင်ဖျက်ပါ။",
                reply_markup=get_main_reply_keyboard()
            )
            return

        context.user_data["awaiting_api_key"] = True
        await update.message.reply_text(
            "🔑 ကျေးဇူးပြု၍ သင်၏ **Gemini API Key** ကို ပေးပို့ပါ:",
            reply_markup=ForceReply(selective=True)
        )
        return

    elif text == "❌ Remove Gemini API":
        context.user_data.pop("gemini_api_key", None)
        context.user_data["awaiting_api_key"] = False
        await update.message.reply_text(
            "🗑️ API Key ကို အောင်မြင်စွာ ဖျက်လိုက်ပါပြီ။",
            reply_markup=get_main_reply_keyboard()
        )
        return

    if context.user_data.get("awaiting_api_key"):
        api_key = text.strip()
        context.user_data["gemini_api_key"] = api_key
        context.user_data["awaiting_api_key"] = False

        try:
            genai.Client(api_key=api_key)
            await update.message.reply_text(
                "✅ API Key သိမ်းဆည်းပြီးပါပြီ။ ဗီဒီယိုဖိုင် ပို့နိုင်ပါပြီ။",
                reply_markup=get_main_reply_keyboard()
            )
        except Exception as e:
            context.user_data.pop("gemini_api_key", None)
            await update.message.reply_text(f"❌ API Key မှားယွင်းနေပါသည်။: {e}", reply_markup=get_main_reply_keyboard())
        return

    if context.user_data.get("awaiting_scene_edit_index") is not None:
        idx = context.user_data["awaiting_scene_edit_index"]
        context.user_data["awaiting_scene_edit_index"] = None

        scenes = context.user_data.get("scenes", [])
        if 0 <= idx < len(scenes):
            cleaned_new_text = text.strip().replace(" ", "")
            scenes[idx]["text"] = cleaned_new_text
            context.user_data["scenes"] = scenes

            success_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Edit Success (အကုန်ပြင်ပြီးပြီ)", callback_data="edit_success")]
            ])

            start_str = format_timestamp(scenes[idx]["start"])
            end_str = format_timestamp(scenes[idx]["end"])

            t_msg = await update.message.reply_text(
                f"✅ **Scene {idx+1}** ကို ပြင်ဆင်ပြီးပါပြီ။\n\n⏱ [{start_str} - {end_str}]\n💬 `{cleaned_new_text}`",
                reply_markup=success_markup,
                parse_mode="Markdown"
            )
            context.user_data["sent_transcript_msg_ids"] = context.user_data.get("sent_transcript_msg_ids", []) + [t_msg.message_id]
            return

    await update.message.reply_text("ကျေးဇူးပြု၍ မီနူးခလုတ်များကို အသုံးပြုပါ သို့မဟုတ် ဗီဒီယိုဖိုင် ပို့ပါ။", reply_markup=get_main_reply_keyboard())


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_activity"] = time.time()

    if not context.user_data.get("gemini_api_key"):
        await update.message.reply_text("⚠️ ပထမဦးစွာ **Gemini API Key** ထည့်ပါ။", reply_markup=get_main_reply_keyboard())
        return

    chat_id = update.effective_chat.id
    pending_path = context.user_data.get("pending_video_path")
    preview_path = context.user_data.get("preview_video_path")
    final_path = context.user_data.get("final_video_path")

    if (pending_path and Path(pending_path).exists()) or (preview_path and Path(preview_path).exists()) or (final_path and Path(final_path).exists()):
        await update.message.reply_text(
            "⚠️ ဆာဗာပေါ်တွင် ဖိုင်ဟောင်းကျန်နေပါသေးသည်။ Clear လုပ်ပါ။",
            reply_markup=get_clear_keyboard()
        )
        return

    file_path = f"video_{chat_id}.mp4"
    status_msg = await update.message.reply_text("⏳ Video ဖိုင်ကို ဒေါင်းလုဒ်ဆွဲနေပါသည်...")

    try:
        if update.message.video:
            file_obj = await update.message.video.get_file()
        elif update.message.video_note:
            file_obj = await update.message.video_note.get_file()
        elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("video/"):
            file_obj = await update.message.document.get_file()
        else:
            await status_msg.edit_text("❌ Video ဖိုင်အမျိုးအစား မမှန်ပါ။", reply_markup=get_main_reply_keyboard())
            return

        await file_obj.download_to_drive(file_path)
        context.user_data["pending_video_path"] = file_path
        context.user_data["waiting_satisfaction"] = True

        await status_msg.edit_text("🎬 Video ရရှိပါပြီ။ အသံအမျိုးအစား ရွေးချယ်ပါ:", reply_markup=get_voice_keyboard())
    except Exception as exc:
        logger.exception("Error downloading video")
        await status_msg.edit_text(f"❌ Video ဒေါင်းလုဒ်အမှား: {exc}", reply_markup=get_main_reply_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["last_activity"] = time.time()
    data = query.data

    if data == "clear_server_files":
        for key in ["pending_video_path", "preview_video_path", "final_video_path", "scenes", "sent_transcript_msg_ids"]:
            path = context.user_data.get(key)
            if isinstance(path, str) and Path(path).exists():
                try: Path(path).unlink()
                except: pass
            context.user_data.pop(key, None)

        context.user_data["waiting_satisfaction"] = False
        await query.edit_message_text("🗑️ ဆာဗာဖိုင်များ ရှင်းလင်းပြီးပါပြီ။ ဗီဒီယိုအသစ် ပို့နိုင်ပါပြီ။", reply_markup=get_main_reply_keyboard())
        return

    if data in ("voice_male", "voice_female"):
        selected_voice = VOICE_MALE if data == "voice_male" else VOICE_FEMALE
        context.user_data["selected_voice"] = selected_voice
        await query.edit_message_text("📱 ဗီဒီယို အရွယ်အစား (Aspect Ratio) ရွေးချယ်ပါ:", reply_markup=get_ratio_keyboard())
        return

    if data.startswith("ratio_"):
        ratio_choice = data.removeprefix("ratio_")
        context.user_data["aspect_ratio"] = ratio_choice
        await query.edit_message_text("⚡ အသံအမြန်နှုန်း (Voice Speed) ရွေးချယ်ပါ:", reply_markup=get_speed_keyboard())
        return

    if data == "open_speed_change":
        await query.message.reply_text("⚡ အမြန်နှုန်း အသစ်ရွေးချယ်ပါ:", reply_markup=get_speed_keyboard())
        return

    if data.startswith("speed_"):
        file_path = context.user_data.get("pending_video_path")
        if not file_path or not Path(file_path).exists():
            await query.edit_message_text("⚠️ Video ဖိုင် မတွေ့ပါ။")
            return

        speed_multiplier = float(data.removeprefix("speed_"))
        context.user_data["speed_multiplier"] = speed_multiplier
        scenes = context.user_data.get("scenes")

        if not scenes:
            selected_voice = context.user_data.get("selected_voice", VOICE_MALE)
            aspect_ratio = context.user_data.get("aspect_ratio", "original")
            await query.edit_message_text(f"🚀 Preview Video ဖန်တီးနေပါပြီ ({speed_multiplier}x)...")
            await generate_preview_and_transcripts(query, context, file_path, selected_voice, speed_multiplier, aspect_ratio, is_callback=True)
            return

        await query.edit_message_text(f"⚡ အမြန်နှုန်း ({speed_multiplier}x) ဖြင့် ဗီဒီယို ပြန်လည်တည်ဆောက်နေပါပြီ...")
        await render_with_current_scenes_and_speed(update, context, speed_multiplier)
        return

    if data == "open_transcript_edit":
        scenes = context.user_data.get("scenes", [])
        chat_id = query.message.chat_id
        sent_transcript_msg_ids = []

        for idx, scene in enumerate(scenes):
            start_str = format_timestamp(scene["start"])
            end_str = format_timestamp(scene["end"])
            text_snippet = scene["text"]

            markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"✏️ Edit Scene {idx+1}", callback_data=f"edit_scene_{idx}")]])
            t_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏱ **Scene {idx+1}** [{start_str} - {end_str}]\n💬 {text_snippet}",
                reply_markup=markup,
                parse_mode="Markdown"
            )
            sent_transcript_msg_ids.append(t_msg.message_id)

        context.user_data["sent_transcript_msg_ids"] = sent_transcript_msg_ids
        await query.message.reply_text("👇 လိုအပ်သော Scene ကိုနှိပ်ပြီး ပြင်ဆင်နိုင်ပါသည်။ အားလုံးပြီးလျှင် **Edit Success** နှိပ်ပါ။")
        return

    if data.startswith("edit_scene_"):
        idx = int(data.removeprefix("edit_scene_"))
        context.user_data["awaiting_scene_edit_index"] = idx
        scenes = context.user_data.get("scenes", [])
        current_text = scenes[idx]["text"] if 0 <= idx < len(scenes) else ""

        await query.message.reply_text(
            f"✏️ **Scene {idx+1}** စာသားအသစ် ပို့ပေးပါ:\n\nလက်ရှိ: `{current_text}`",
            parse_mode="Markdown",
            reply_markup=ForceReply(selective=True)
        )
        return

    if data == "edit_success":
        await query.edit_message_text("✅ ဗီဒီယိုအသစ် စတင်တည်ဆောက်နေပါပြီ...")
        await finalize_and_render_video(update, context)
        return

    if data == "user_satisfied":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        for key in ["pending_video_path", "preview_video_path", "final_video_path", "scenes", "sent_transcript_msg_ids"]:
            path = context.user_data.get(key)
            if isinstance(path, str) and Path(path).exists():
                try: Path(path).unlink()
                except: pass
            context.user_data.pop(key, None)

        context.user_data["waiting_satisfaction"] = False
        await query.message.reply_text("👍 ကျေးဇူးတင်ပါသည်။ ဗီဒီယိုသိမ်းဆည်းပြီးပါပြီ။ နောက်ထပ် ဗီဒီယို ပို့နိုင်ပါသည်။")
        return


# -----------------------------------------------------------------------------
# Background Task
# -----------------------------------------------------------------------------
async def inactivity_cleanup_task(application: Application):
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for chat_id, user_data in list(application.chat_data.items()):
            last_act = user_data.get("last_activity")
            if last_act and (now - last_act > INACTIVITY_LIMIT):
                for key in ["pending_video_path", "preview_video_path", "final_video_path"]:
                    path = user_data.get(key)
                    if path and Path(path).exists():
                        try: Path(path).unlink()
                        except: pass
                user_data.clear()


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO, handle_video))

    async def post_init(application: Application):
        asyncio.create_task(inactivity_cleanup_task(application))
        logger.info("Inactivity cleanup background worker started.")

    app.post_init = post_init
    logger.info("Bot running with Burmese Subtitles (ASS Burn-in) support.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
