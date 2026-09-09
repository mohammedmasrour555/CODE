import os
import time
import shutil
import threading
import io
import av
import httpx
import m3u8
from git import Repo

# --- الإعدادات ---
GITHUB_USER = "mohammedmasrour555"
REPO_NAME = "hhh"
BRANCH_NAME = "main"
LOCAL_DIR = "./local_hls_split"
SOURCE_M3U8 = "https://d2qh3gh0k5vp3v.cloudfront.net/v1/master/3722c60a815c199d9c0ef36c5b73da68a62b09d1/cc-n6pess5lwbghr/2M_ES.m3u8"

HLS_LIST_SIZE = 4  # الاحتفاظ بـ 4 قطع فقط مثل FFmpeg

# --- 1. إعداد مسار وصلاحيات مفتاح SSH ---
PROJECT_DIR = os.path.abspath("./")
SSH_KEY_PATH = os.path.join(PROJECT_DIR, ".my_git_keys", "id_ed25519")

if os.path.exists(SSH_KEY_PATH):
    try:
        os.chmod(SSH_KEY_PATH, 0o600)
    except Exception:
        pass

os.environ["GIT_SSH_COMMAND"] = f'ssh -i "{SSH_KEY_PATH}" -o StrictHostKeyChecking=no'
REMOTE_SSH_URL = f"git@github.com:{GITHUB_USER}/{REPO_NAME}.git"

# --- 2. تهيئة المستودع دون تعليق الحذف ---
os.makedirs(LOCAL_DIR, exist_ok=True)

# تنظيف الملفات القديمة بسرعة ودون حذف مجلد .git
for f in os.listdir(LOCAL_DIR):
    if f != ".git":
        file_path = os.path.join(LOCAL_DIR, f)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path, ignore_errors=True)
        except Exception:
            pass

# تهيئة أو إعادة ربط Git
try:
    repo = Repo(LOCAL_DIR)
except Exception:
    repo = Repo.init(LOCAL_DIR)

repo.config_writer().set_value("user", "email", f"{GITHUB_USER}@gmail.com").release()
repo.config_writer().set_value("user", "name", GITHUB_USER).release()

if 'origin' in [r.name for r in repo.remotes]:
    origin = repo.remote('origin')
    origin.set_url(REMOTE_SSH_URL)
else:
    origin = repo.create_remote('origin', REMOTE_SSH_URL)

try:
    repo.git.branch('-M', BRANCH_NAME)
except Exception:
    pass


# --- 3. محرك PyAV (بديل FFmpeg) لـ Binary Remuxing فـ الـ Memory ---
def remux_segment_bytes(input_bytes: bytes) -> bytes:
    """معالجة الـ TS Segment فـ Binary Level بدون Re-encoding"""
    try:
        in_buf = io.BytesIO(input_bytes)
        out_buf = io.BytesIO()

        container_in = av.open(in_buf, format='mpegts')
        container_out = av.open(out_buf, mode='w', format='mpegts')

        stream_map = {}
        for stream in container_in.streams:
            out_stream = container_out.add_stream(template=stream)
            stream_map[stream.index] = out_stream

        for packet in container_in.demux():
            if packet.stream.index in stream_map:
                packet.stream = stream_map[packet.stream.index]
                container_out.mux(packet)

        container_out.close()
        return out_buf.getvalue()
    except Exception:
        # إذا حدث خطأ بسيط نرجع البيانات كما هي
        return input_bytes


def run_pyav_hls_downloader():
    print("🎬 بدء محرك التقطيع PyAV (بدون FFmpeg)...")
    
    client = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=10.0,
        follow_redirects=True
    )
    
    processed_segments = set()
    segments_ring = [] # للحفاظ على عدد المقطوعات ومسح القديم
    media_sequence = 0

    while True:
        try:
            # 1. جلب הـ m3u8 الأصلي
            res = client.get(SOURCE_M3U8)
            if res.status_code != 200:
                time.sleep(2)
                continue

            parsed = m3u8.loads(res.text, uri=SOURCE_M3U8)
            
            # إذا كان Master Playlist، خذ أول Sub-playlist
            if parsed.is_variant:
                sub_url = parsed.playlists[0].absolute_uri
                res = client.get(sub_url)
                parsed = m3u8.loads(res.text, uri=sub_url)

            new_segments_found = False

            for seg in parsed.segments:
                seg_url = seg.absolute_uri
                if seg_url in processed_segments:
                    continue

                # 2. تحميل الـ Segment معالجة بالـ Remuxing فـ RAM
                seg_res = client.get(seg_url)
                if seg_res.status_code == 200:
                    remuxed_bytes = remux_segment_bytes(seg_res.content)
                    
                    seg_filename = f"segment_{int(time.time()*1000)}.ts"
                    seg_filepath = os.path.join(LOCAL_DIR, seg_filename)
                    
                    # كتابة الملف للديسك للرفع
                    with open(seg_filepath, "wb") as f:
                        f.write(remuxed_bytes)

                    segments_ring.append({
                        "filename": seg_filename,
                        "duration": seg.duration,
                        "filepath": seg_filepath
                    })
                    processed_segments.add(seg_url)
                    new_segments_found = True

            # 3. تطبيق نظام delete_segments (حذف القطع القديمة لتوفير المساحة)
            while len(segments_ring) > HLS_LIST_SIZE:
                old_seg = segments_ring.pop(0)
                if os.path.exists(old_seg["filepath"]):
                    try:
                        os.remove(old_seg["filepath"])
                    except Exception:
                        pass
                media_sequence += 1

            # 4. بناء وتحديث index.m3u8 المحلي
            if new_segments_found and segments_ring:
                target_duration = max([int(s["duration"]) + 1 for s in segments_ring] or [5])
                m3u8_content = "#EXTM3U\n"
                m3u8_content += "#EXT-X-VERSION:3\n"
                m3u8_content += f"#EXT-X-TARGETDURATION:{target_duration}\n"
                m3u8_content += f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n"

                for s in segments_ring:
                    m3u8_content += f"#EXTINF:{s['duration']:.3f},\n"
                    m3u8_content += f"{s['filename']}\n"

                with open(os.path.join(LOCAL_DIR, "index.m3u8"), "w") as f:
                    f.write(m3u8_content)

        except Exception as e:
            print(f"[!] خطأ فـ محرك التقطيع: {e}")

        time.sleep(3)


# --- 4. محرك الرفع المباشر عبر SSH ---
def github_uploader_loop():
    print("🚀 بدء الرفع التلقائي لـ GitHub عبر SSH...")
    while True:
        try:
            repo.git.add(A=True)
            if repo.is_dirty(untracked_files=True):
                commit_msg = f"Update HLS {time.time()}"
                repo.index.commit(commit_msg)
                repo.git.push('origin', BRANCH_NAME, force=True)
                print(f"[✓] تم رفع التحديث بنجاح في {time.strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[!] خطأ أثناء الرفع: {e}")
        time.sleep(3)


# تشغيل الخيوط
threading.Thread(target=run_pyav_hls_downloader, daemon=True).start()
time.sleep(3)
threading.Thread(target=github_uploader_loop, daemon=True).start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n🛑 تم إيقاف البث والرفع بنجاح.")
