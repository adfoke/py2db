from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
from datetime import datetime, timedelta, timezone
import os
import re
import requests
import threading
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Change this in production

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'twitter.db')
MEDIA_DIR = os.path.join(BASE_DIR, 'static', 'media')
EXPORTS_DIR = os.path.join(BASE_DIR, 'exports')
JOB_TTL_MINUTES = 120
SEARCH_PER_PAGE = 20
MEDIA_URL_PREFIX = '/static/media/'
TCO_URL_RE = re.compile(r'https?://t\.co/\S+')
UTC = timezone.utc
UTC_PLUS_8 = timezone(timedelta(hours=8))


@app.template_filter('utc8')
def utc8_filter(value):
    return format_utc8(value)


def normalize_message_text(value):
    raw_html = (value or '').replace('\x00', '')
    text = BeautifulSoup(raw_html, 'html.parser').get_text('\n')
    text = text.replace('\xa0', ' ').replace('\r', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


@app.template_filter('display_text')
def display_text_filter(value):
    text = normalize_message_text(value)
    return text.replace('答：', '\n答：')

# Base URLs for images and videos
IMG_BASE_URL = 'https://oneos.20110814.org/'
VID_BASE_URL = 'https://oneos.20110814.org/'


def build_upload_filename(tweet_id, original_name, fallback_ext=''):
    safe_name = secure_filename(original_name or '')
    if not safe_name:
        safe_name = f'{uuid.uuid4().hex}{fallback_ext}'
    return f'{tweet_id}_{safe_name}'


def normalize_local_media_reference(path):
    if not path.startswith(MEDIA_URL_PREFIX):
        return None
    filename = os.path.basename(path.split(MEDIA_URL_PREFIX, 1)[-1].strip())
    if not filename:
        return None
    filepath = os.path.join(MEDIA_DIR, filename)
    return filename if os.path.isfile(filepath) else None


def split_media_paths(value):
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def is_inline_emoji_asset(filename):
    name = (filename or '').strip().lower()
    return bool(
        re.match(r'^[a-z]{1,10}_[a-z0-9_]+-[a-f0-9]{8,}\.(png|gif|jpg|jpeg|webp)$', name)
        or name == 'timeline_card_small_video_default.png'
    )


def clean_message_text(value):
    text = normalize_message_text(value).replace('\n', ' ')
    text = TCO_URL_RE.sub('', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def build_import_text(value):
    raw_text = normalize_message_text(value).replace('\n', ' ')
    cleaned_text = clean_message_text(raw_text)
    if cleaned_text:
        return cleaned_text
    return re.sub(r'\s{2,}', ' ', raw_text).strip()


def parse_storage_datetime(value):
    text = (value or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S %z', '%Y/%m/%d %H:%M:%S %z'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_created_at_for_storage(value):
    text = (value or '').replace('\x00', '').strip()
    if not text:
        return text
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    dt = parse_storage_datetime(text)
    if dt is None:
        return text
    if dt.tzinfo is None:
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    return dt.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def format_utc8(value):
    dt = parse_storage_datetime(value)
    if dt is None:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC_PLUS_8).strftime('%Y-%m-%d %H:%M:%S')


def utc8_input_to_storage(value):
    text = (value or '').strip()
    dt = parse_storage_datetime(text)
    if dt is None:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_PLUS_8)
    return dt.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def now_utc8_text():
    return datetime.now(UTC_PLUS_8).strftime('%Y-%m-%d %H:%M:%S')


def utc8_date_to_storage_boundary(date_text, end_of_day=False):
    dt = datetime.strptime(date_text, '%Y-%m-%d')
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.replace(tzinfo=UTC_PLUS_8).astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def utc8_date_range_to_storage_bounds(start_date, end_date):
    return (
        utc8_date_to_storage_boundary(start_date),
        utc8_date_to_storage_boundary(end_date, end_of_day=True),
    )


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)
    return path


def get_return_url(default_endpoint='index'):
    next_url = (request.form.get('next') or request.args.get('next') or '').strip()
    if next_url.startswith('/'):
        return next_url

    referrer = (request.referrer or '').strip()
    if referrer:
        parsed = urlparse(referrer)
        current_host = urlparse(request.host_url).netloc
        if not parsed.netloc or parsed.netloc == current_host:
            path = parsed.path or '/'
            if parsed.query:
                path = f'{path}?{parsed.query}'
            return path

    return url_for(default_endpoint)


def build_tweet_form_data(source=None):
    source = source or {}
    return {
        'user': (source.get('user') or '').strip(),
        'text': source.get('text') or '',
        'created_at': (source.get('created_at') or '').strip(),
        'image_urls': source.get('image_urls') or '',
        'video_urls': source.get('video_urls') or '',
    }


def collect_media_inputs(tweet_id, image_paths=None, video_paths=None):
    ensure_directory(MEDIA_DIR)
    image_paths = list(image_paths or [])
    video_paths = list(video_paths or [])

    images = request.files.getlist('images')
    for img in images:
        if img and img.filename:
            filename = build_upload_filename(tweet_id, img.filename)
            filepath = os.path.join(MEDIA_DIR, filename)
            img.save(filepath)
            image_paths.append(filename)

    videos = request.files.getlist('videos')
    for vid in videos:
        if vid and vid.filename:
            filename = build_upload_filename(tweet_id, vid.filename)
            filepath = os.path.join(MEDIA_DIR, filename)
            vid.save(filepath)
            video_paths.append(filename)

    image_urls = request.form.get('image_urls', '').strip().split('\n')
    for url in image_urls:
        url = url.strip()
        if not url:
            continue
        filename = normalize_local_media_reference(url)
        if filename:
            if filename not in image_paths:
                image_paths.append(filename)
            continue
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                content_type = resp.headers.get('content-type', '')
                ext = '.jpg' if 'jpeg' in content_type or 'jpg' in content_type else '.png'
                filename = f"{tweet_id}_url_{len(image_paths)}{ext}"
                filepath = os.path.join(MEDIA_DIR, filename)
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                image_paths.append(filename)
        except Exception:
            pass

    video_urls = request.form.get('video_urls', '').strip().split('\n')
    for url in video_urls:
        url = url.strip()
        if not url:
            continue
        filename = normalize_local_media_reference(url)
        if filename:
            if filename not in video_paths:
                video_paths.append(filename)
            continue
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                filename = f"{tweet_id}_url_{len(video_paths)}.mp4"
                filepath = os.path.join(MEDIA_DIR, filename)
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                video_paths.append(filename)
        except Exception:
            pass

    return image_paths, video_paths


def build_export_name(start_date, end_date, export_name=''):
    custom_name = secure_filename((export_name or '').strip())
    if custom_name:
        return custom_name
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f'messages_{start_date}_to_{end_date}_{timestamp}'


def copy_media_files(filenames, media_type, export_dir):
    if not filenames:
        return []
    target_dir = ensure_directory(os.path.join(export_dir, media_type))
    copied_paths = []
    for filename in filenames:
        safe_name = os.path.basename(filename)
        source_path = os.path.join(MEDIA_DIR, safe_name)
        if not os.path.isfile(source_path):
            continue
        target_path = os.path.join(target_dir, safe_name)
        if not os.path.exists(target_path):
            shutil.copy2(source_path, target_path)
        copied_paths.append(f'{media_type}/{safe_name}')
    return copied_paths


def build_export_markdown(messages):
    lines = [
        '# 消息导出',
        '',
        f'- 导出时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 消息数量: {len(messages)}',
        '',
    ]
    for message in messages:
        lines.extend([
            f'created_at: {format_utc8(message["created_at"])}',
            f'user: {message["user"]}',
            f'text: {message["text"]}',
            '',
        ])
        if message['image_paths']:
            lines.append('image_paths:')
            for image_path in message['image_paths']:
                image_name = os.path.basename(image_path)
                lines.append(f'- [{image_name}]({image_path})')
                lines.append(f'  ![{image_name}]({image_path})')
            lines.append('')
        if message['video_paths']:
            lines.append('video_paths:')
            for video_path in message['video_paths']:
                video_name = os.path.basename(video_path)
                lines.append(f'- [{video_name}]({video_path})')
            lines.append('')
        lines.append('---')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def export_messages_to_markdown(start_date, end_date, export_name=''):
    export_folder_name = build_export_name(start_date, end_date, export_name)
    export_dir = ensure_directory(os.path.join(EXPORTS_DIR, export_folder_name))
    markdown_path = os.path.join(export_dir, 'export.md')
    query_start, query_end = utc8_date_range_to_storage_bounds(start_date, end_date)

    conn = get_db_connection()
    messages = conn.execute(
        '''
        SELECT * FROM tweets
        WHERE created_at >= ? AND created_at <= ?
        ORDER BY created_at ASC, tweet_id ASC
        ''',
        (query_start, query_end)
    ).fetchall()
    conn.close()

    if not messages:
        return None

    exported_messages = []
    image_count = 0
    video_count = 0
    for message in messages:
        image_paths = copy_media_files(split_media_paths(message['image_paths']), 'images', export_dir)
        video_paths = copy_media_files(split_media_paths(message['video_paths']), 'videos', export_dir)
        image_count += len(image_paths)
        video_count += len(video_paths)
        exported_messages.append({
            'tweet_id': message['tweet_id'],
            'user': message['user'],
            'text': normalize_message_text(message['text']),
            'created_at': message['created_at'],
            'image_paths': image_paths,
            'video_paths': video_paths,
        })

    with open(markdown_path, 'w', encoding='utf-8') as handle:
        handle.write(build_export_markdown(exported_messages))

    return {
        'export_dir': export_dir,
        'markdown_path': markdown_path,
        'message_count': len(exported_messages),
        'image_count': image_count,
        'video_count': video_count,
    }

def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute('''CREATE TABLE IF NOT EXISTS tweets (
        tweet_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        image_paths TEXT DEFAULT "",
        video_paths TEXT DEFAULT ""
    )''')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_tweets_unique ON tweets(user, created_at, text)')
    conn.execute('''CREATE TABLE IF NOT EXISTS import_jobs (
        job_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        start_page INTEGER NOT NULL,
        end_page INTEGER NOT NULL,
        current_page INTEGER NOT NULL,
        processed_pages INTEGER NOT NULL,
        total_pages INTEGER NOT NULL,
        imported_count INTEGER NOT NULL,
        skipped_count INTEGER NOT NULL,
        status_message TEXT,
        error TEXT,
        url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_updated TEXT NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_import_jobs_last_updated ON import_jobs(last_updated)')
    # Add columns if not exist
    try:
        conn.execute('ALTER TABLE tweets ADD COLUMN image_paths TEXT DEFAULT ""')
    except:
        pass
    try:
        conn.execute('ALTER TABLE tweets ADD COLUMN video_paths TEXT DEFAULT ""')
    except:
        pass
    try:
        conn.execute('ALTER TABLE import_jobs ADD COLUMN url TEXT DEFAULT "https://20110814.org/api/gengxin?type=all&page={page}"')
    except:
        pass
    try:
        conn.execute('ALTER TABLE import_jobs ADD COLUMN img_base_url TEXT DEFAULT "https://oneos.20110814.org/"')
    except:
        pass
    try:
        conn.execute('ALTER TABLE import_jobs ADD COLUMN vid_base_url TEXT DEFAULT "https://oneos.20110814.org/"')
    except:
        pass
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def cleanup_expired_jobs(conn=None):
    owns_connection = conn is None
    if owns_connection:
        conn = get_db_connection()
    cutoff = (datetime.utcnow() - timedelta(minutes=JOB_TTL_MINUTES)).isoformat()
    try:
        conn.execute('DELETE FROM import_jobs WHERE last_updated < ?', (cutoff,))
        if owns_connection:
            conn.commit()
    except sqlite3.OperationalError:
        pass  # Ignore if database is locked
    finally:
        if owns_connection:
            conn.close()


def create_import_job(start_page, end_page, url='https://20110814.org/api/gengxin?type=all&page={page}', img_base_url='https://oneos.20110814.org/', vid_base_url='https://oneos.20110814.org/'):
    cleanup_expired_jobs()
    job_id = str(uuid.uuid4())
    total_pages = end_page - start_page + 1
    now = datetime.utcnow().isoformat()
    conn = get_db_connection()
    conn.execute(
        '''INSERT INTO import_jobs (
            job_id, status, start_page, end_page, current_page, processed_pages,
            total_pages, imported_count, skipped_count, status_message, error, url,
            img_base_url, vid_base_url, created_at, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            job_id,
            'pending',
            start_page,
            end_page,
            start_page,
            0,
            total_pages,
            0,
            0,
            '',
            None,
            url,
            img_base_url,
            vid_base_url,
            now,
            now
        )
    )
    conn.commit()
    conn.close()
    return job_id


def get_job(job_id):
    conn = get_db_connection()
    cleanup_expired_jobs(conn)
    job = conn.execute('SELECT * FROM import_jobs WHERE job_id = ?', (job_id,)).fetchone()
    conn.close()
    return dict(job) if job else None


def update_job(job_id, **kwargs):
    if not kwargs:
        return get_job(job_id)
    fields = []
    values = []
    for key, value in kwargs.items():
        fields.append(f"{key} = ?")
        values.append(value)
    fields.append('last_updated = ?')
    values.append(datetime.utcnow().isoformat())
    values.append(job_id)
    conn = get_db_connection()
    conn.execute(f"UPDATE import_jobs SET {', '.join(fields)} WHERE job_id = ?", values)
    conn.commit()
    conn.close()
    return get_job(job_id)


def download_media(media_url, filepath):
    """Download media file to the specified path, overwriting if it exists."""
    # Try with SSL verification first, then without if it fails
    for verify_ssl in [True, False]:
        try:
            resp = requests.get(media_url, timeout=10, verify=verify_ssl)
            if resp.status_code == 200:
                # Ensure directory exists
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                # Overwrite existing file
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                return True
        except Exception:
            continue
    return False


def perform_import(start_page, end_page, job_id=None):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    imported_count = 0
    skipped_count = 0
    pages = list(range(start_page, end_page + 1))
    conn = None
    try:
        conn = get_db_connection()
        job = get_job(job_id) if job_id else None
        base_url = job['url'] if job else 'https://20110814.org/api/gengxin?type=all&page={page}'
        img_base_url = job['img_base_url'] if job else IMG_BASE_URL
        vid_base_url = job['vid_base_url'] if job else VID_BASE_URL

        # Use ThreadPoolExecutor for concurrent media downloads
        with ThreadPoolExecutor(max_workers=5) as executor:
            for index, page in enumerate(pages, start=1):
                if job_id:
                    update_job(job_id, status='running', current_page=page, processed_pages=index - 1)
                url = base_url.format(page=page)
                try:
                    # Try with SSL verification first, then without if it fails
                    response = None
                    for verify_ssl in [True, False]:
                        try:
                            response = requests.get(url, timeout=10, verify=verify_ssl)
                            response.raise_for_status()
                            break
                        except Exception:
                            continue
                    if response is None:
                        raise RuntimeError('Failed to fetch page after trying both SSL modes')
                    data = response.json()
                except Exception as exc:
                    message = f'Error on page {page}: {exc}'
                    raise RuntimeError(message) from exc
                posts = data.get('date_list', [])

                # Initialize post media tracking
                post_images = {i: [] for i in range(len(posts))}
                post_videos = {i: [] for i in range(len(posts))}

                # Collect all media download tasks with metadata
                download_tasks = []
                media_info = []

                for post_idx, post in enumerate(posts):
                    def normalize_media_filename(url_or_name):
                        if not url_or_name:
                            return None
                        name = (
                            url_or_name.split('?')[0]
                            .split('#')[0]
                        )
                        name = name.replace('..', '').strip('/')
                        return name or None

                    user_name_full = post.get('user_name', '').replace('\x00', '')
                    user = user_name_full.split(' @')[0] if ' @' in user_name_full else user_name_full
                    raw_text = post.get('text_show', '').replace('\x00', '')
                    text = build_import_text(raw_text)
                    created_at = normalize_created_at_for_storage(post.get('created_s', ''))

                    # Prepare image downloads from pic_ids
                    pic_ids_str = (post.get('pic_ids') or '').strip()
                    if pic_ids_str:
                        raw_filenames = [f.strip() for f in pic_ids_str.split(',') if f.strip()]
                        pic_filenames = []
                        for raw in raw_filenames:
                            filename = normalize_media_filename(raw)
                            if not filename:
                                continue
                            if not os.path.splitext(filename)[1]:
                                filename = f'{filename}.jpg'
                            filepath = os.path.join(MEDIA_DIR, filename)
                            img_url = raw if raw.startswith('http') else f"{img_base_url}{filename}"
                            task = executor.submit(download_media, img_url, filepath)
                            download_tasks.append(task)
                            media_info.append({
                                'post_idx': post_idx,
                                'type': 'image',
                                'filename': filename,
                                'task': task
                            })
                            pic_filenames.append(filename)

                    # Prepare image downloads from embedded images in text
                    soup = BeautifulSoup(raw_text, 'html.parser')
                    img_tags = soup.find_all('img')
                    for img in img_tags:
                        src = img.get('src')
                        if src and src.startswith('http'):
                            filename = normalize_media_filename(src.split('/')[-1])
                            if filename and not is_inline_emoji_asset(filename):
                                filepath = os.path.join(MEDIA_DIR, filename)
                                task = executor.submit(download_media, src, filepath)
                                download_tasks.append(task)
                                media_info.append({
                                    'post_idx': post_idx,
                                    'type': 'image',
                                    'filename': filename,
                                    'task': task
                                })

                    # Prepare video downloads
                    video_ids_str = (post.get('video_ids') or '').strip()
                    if video_ids_str:
                        raw_videos = [f.strip() for f in video_ids_str.split(',') if f.strip()]
                        for raw in raw_videos:
                            filename = normalize_media_filename(raw)
                            if not filename:
                                continue
                            vid_url = raw if raw.startswith('http') else f"{vid_base_url}{filename}"
                            filepath = os.path.join(MEDIA_DIR, filename)
                            task = executor.submit(download_media, vid_url, filepath)
                            download_tasks.append(task)
                            media_info.append({
                                'post_idx': post_idx,
                                'type': 'video',
                                'filename': filename,
                                'task': task
                            })

                # Wait for all downloads to complete and collect successful ones
                for task in as_completed(download_tasks):
                    success = task.result()
                    # Find which media this task corresponds to
                    for info in media_info:
                        if info['task'] == task:
                            if success:
                                if info['type'] == 'image':
                                    post_images[info['post_idx']].append(info['filename'])
                                elif info['type'] == 'video':
                                    post_videos[info['post_idx']].append(info['filename'])
                            break

                # Insert posts into database
                for post_idx, post in enumerate(posts):
                    user_name_full = post.get('user_name', '').replace('\x00', '')
                    user = user_name_full.split(' @')[0] if ' @' in user_name_full else user_name_full
                    text = build_import_text(post.get('text_show', ''))
                    created_at = normalize_created_at_for_storage(post.get('created_s', ''))

                    image_paths_str = ','.join(post_images[post_idx])
                    video_paths_str = ','.join(post_videos[post_idx])

                    if user and created_at and (text or image_paths_str or video_paths_str):
                        # Check if tweet already exists
                        cursor = conn.execute(
                            'SELECT tweet_id FROM tweets WHERE user = ? AND created_at = ? AND text = ?',
                            (user, created_at, text)
                        )
                        existing = cursor.fetchone()
                        if existing:
                            # Update existing tweet with media paths
                            conn.execute(
                                'UPDATE tweets SET image_paths = ?, video_paths = ? WHERE tweet_id = ?',
                                (image_paths_str, video_paths_str, existing['tweet_id'])
                            )
                            skipped_count += 1  # Count as skipped since not newly imported
                        else:
                            # Insert new tweet
                            conn.execute(
                                'INSERT INTO tweets (user, text, created_at, image_paths, video_paths) VALUES (?, ?, ?, ?, ?)',
                                (user, text, created_at, image_paths_str, video_paths_str)
                            )
                            imported_count += 1

                conn.commit()
                if job_id:
                    update_job(
                        job_id,
                        processed_pages=index,
                        imported_count=imported_count,
                        skipped_count=skipped_count,
                        status_message=f'已处理 {index} / {len(pages)} 页'
                    )
    finally:
        if conn:
            conn.close()
    return imported_count, skipped_count


def run_import_job(job_id, start_page, end_page):
    try:
        imported_count, skipped_count = perform_import(start_page, end_page, job_id=job_id)
        update_job(
            job_id,
            status='completed',
            processed_pages=end_page - start_page + 1,
            imported_count=imported_count,
            skipped_count=skipped_count,
            status_message=f'已导入 {imported_count} 条，跳过 {skipped_count} 条重复数据'
        )
    except Exception as exc:
        update_job(job_id, status='error', error=str(exc), status_message=str(exc))


@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    onlymedia = request.args.get('onlymedia', '').strip() in ('1', 'true', 'on')
    per_page = 20
    offset = (page - 1) * per_page

    where_sql = ''
    params = []
    if onlymedia:
        where_sql = "WHERE (COALESCE(image_paths, '') != '' OR COALESCE(video_paths, '') != '')"

    conn = get_db_connection()
    tweets = conn.execute(
        f'SELECT * FROM tweets {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?',
        [*params, per_page, offset]
    ).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM tweets {where_sql}', params).fetchone()[0]
    conn.close()

    total_pages = (total + per_page - 1) // per_page
    return render_template(
        'index.html',
        tweets=tweets,
        page=page,
        total_pages=total_pages,
        total=total,
        onlymedia=onlymedia,
    )

@app.route('/search', methods=['GET', 'POST'])
def search():
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        onlymedia = '1' if request.form.get('onlymedia') else ''
        return redirect(url_for('search', query=query, start_date=start_date, end_date=end_date, onlymedia=onlymedia, page=1))

    page = max(request.args.get('page', 1, type=int), 1)
    query = request.args.get('query', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    onlymedia = request.args.get('onlymedia', '').strip() in ('1', 'true', 'on')
    has_filters = any([query, start_date, end_date, onlymedia])

    if not has_filters:
        return render_template(
            'search.html',
            query=query,
            start_date=start_date,
            end_date=end_date,
            onlymedia=onlymedia,
            has_filters=False,
            result_count=0,
            page=1,
            total_pages=0,
        )

    offset = (page - 1) * SEARCH_PER_PAGE
    where_clauses = ['1=1']
    params = []

    if query:
        where_clauses.append('(user LIKE ? OR text LIKE ?)')
        params.extend([f'%{query}%', f'%{query}%'])

    if start_date:
        where_clauses.append('created_at >= ?')
        params.append(utc8_date_to_storage_boundary(start_date))

    if end_date:
        where_clauses.append('created_at <= ?')
        params.append(utc8_date_to_storage_boundary(end_date, end_of_day=True))

    if onlymedia:
        where_clauses.append("(COALESCE(image_paths, '') != '' OR COALESCE(video_paths, '') != '')")

    where_sql = ' AND '.join(where_clauses)
    count_sql = f'SELECT COUNT(*) FROM tweets WHERE {where_sql}'
    query_sql = f'''
        SELECT * FROM tweets
        WHERE {where_sql}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    '''

    conn = get_db_connection()
    total = conn.execute(count_sql, params).fetchone()[0]
    tweets = conn.execute(query_sql, [*params, SEARCH_PER_PAGE, offset]).fetchall()
    conn.close()

    total_pages = (total + SEARCH_PER_PAGE - 1) // SEARCH_PER_PAGE
    return render_template(
        'search.html',
        tweets=tweets,
        query=query,
        start_date=start_date,
        end_date=end_date,
        onlymedia=onlymedia,
        result_count=total,
        has_filters=True,
        page=page,
        total_pages=total_pages,
    )

@app.route('/import', methods=['GET'])
def import_data():
    return render_template('import.html')


@app.route('/export', methods=['GET', 'POST'])
def export_messages():
    export_result = None
    start_date = ''
    end_date = ''
    export_name = ''
    if request.method == 'POST':
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        export_name = request.form.get('export_name', '').strip()
        if not start_date or not end_date:
            flash('请填写开始日期和结束日期。')
        elif start_date > end_date:
            flash('开始日期不能晚于结束日期。')
        else:
            export_result = export_messages_to_markdown(start_date, end_date, export_name)
            if export_result is None:
                flash('该时间范围内没有可导出的消息。')
            else:
                flash('导出完成。')
    return render_template(
        'export.html',
        export_result=export_result,
        start_date=start_date,
        end_date=end_date,
        export_name=export_name,
    )


@app.route('/add', methods=['GET', 'POST'])
def add_tweet():
    form_data = build_tweet_form_data({
        'created_at': now_utc8_text(),
    })

    if request.method == 'POST':
        form_data = build_tweet_form_data(request.form)
        user = form_data['user']
        text = clean_message_text(form_data['text'])
        created_at = utc8_input_to_storage(form_data['created_at'])

        if not user or not text or not created_at:
            flash('请填写用户、内容和创建时间。')
            return render_template('add.html', form_data=form_data)

        conn = get_db_connection()
        try:
            cursor = conn.execute(
                'INSERT INTO tweets (user, text, created_at, image_paths, video_paths) VALUES (?, ?, ?, ?, ?)',
                (user, text, created_at, '', '')
            )
            tweet_id = cursor.lastrowid
            image_paths, video_paths = collect_media_inputs(tweet_id)
            conn.execute(
                'UPDATE tweets SET image_paths = ?, video_paths = ? WHERE tweet_id = ?',
                (','.join(image_paths), ','.join(video_paths), tweet_id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash('已存在相同用户、时间和内容的消息。')
            return render_template('add.html', form_data=form_data)
        finally:
            if conn:
                conn.close()

        flash('消息已创建')
        return redirect(url_for('index'))

    return render_template('add.html', form_data=form_data)

@app.route('/edit/<int:tweet_id>', methods=['GET', 'POST'])
def edit_tweet(tweet_id):
    conn = get_db_connection()
    tweet = conn.execute('SELECT * FROM tweets WHERE tweet_id = ?', (tweet_id,)).fetchone()
    if not tweet:
        flash('消息不存在')
        return redirect(url_for('index'))
    if request.method == 'POST':
        user = request.form['user']
        text = clean_message_text(request.form['text'])
        created_at = utc8_input_to_storage(request.form['created_at'])
        image_paths, video_paths = collect_media_inputs(
            tweet_id,
            split_media_paths(tweet['image_paths']),
            split_media_paths(tweet['video_paths'])
        )
        image_paths_str = ','.join(image_paths)
        video_paths_str = ','.join(video_paths)
        conn.execute('UPDATE tweets SET user = ?, text = ?, created_at = ?, image_paths = ?, video_paths = ? WHERE tweet_id = ?',
                     (user, text, created_at, image_paths_str, video_paths_str, tweet_id))
        conn.commit()
        conn.close()
        flash('消息已更新')
        return redirect(url_for('index'))
    conn.close()
    return render_template('edit.html', tweet=tweet)

@app.route('/delete/<int:tweet_id>', methods=['POST'])
def delete_tweet(tweet_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM tweets WHERE tweet_id = ?', (tweet_id,))
    conn.commit()
    conn.close()
    flash('消息已删除')
    return redirect(get_return_url())

@app.route('/batch_delete', methods=['POST'])
def batch_delete():
    selected = request.form.getlist('selected')
    if selected:
        conn = get_db_connection()
        deleted_count = 0
        for tweet_id in selected:
            cursor = conn.execute('DELETE FROM tweets WHERE tweet_id = ?', (tweet_id,))
            deleted_count += cursor.rowcount
        conn.commit()
        conn.close()
        flash(f'已删除 {deleted_count} 条消息')
    else:
        flash('未选择任何消息')
    return redirect(get_return_url())


@app.route('/clear_database', methods=['POST'])
def clear_database():
    conn = get_db_connection()
    conn.execute('DELETE FROM tweets')
    conn.execute('DELETE FROM import_jobs')
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tweets'")
    conn.commit()
    conn.close()

    vacuum_conn = sqlite3.connect(DATABASE)
    vacuum_conn.execute('VACUUM')
    vacuum_conn.close()

    if os.path.isdir(MEDIA_DIR):
        for entry in os.listdir(MEDIA_DIR):
            file_path = os.path.join(MEDIA_DIR, entry)
            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    flash('数据库内容和媒体文件已全部清空。')
    return redirect(url_for('index'))

def _should_return_json():
    requested_with = request.headers.get('X-Requested-With', '').lower()
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept or requested_with == 'xmlhttprequest'


@app.route('/import_web', methods=['POST'])
def import_web():
    try:
        start_page = int(request.form.get('start_page', 1))
        end_page = int(request.form.get('end_page', 10))
        url = request.form.get('url', 'https://20110814.org/api/gengxin?type=all&page={page}')
        img_base_url = request.form.get('img_base_url', 'https://oneos.20110814.org/')
        vid_base_url = request.form.get('vid_base_url', 'https://oneos.20110814.org/')
    except (TypeError, ValueError):
        message = '页码格式不正确。'
        if _should_return_json():
            return jsonify({'success': False, 'message': message}), 400
        flash(message)
        return redirect(url_for('import_data'))

    if start_page > end_page or start_page < 1:
        message = '页码范围无效，确保起始页和结束页都大于等于 1，且起始页不晚于结束页。'
        if _should_return_json():
            return jsonify({'success': False, 'message': message}), 400
        flash(message)
        return redirect(url_for('import_data'))

    job_id = create_import_job(start_page, end_page, url, img_base_url, vid_base_url)
    thread = threading.Thread(target=run_import_job, args=(job_id, start_page, end_page), daemon=True)
    thread.start()

    if _should_return_json():
        return jsonify({'success': True, 'job_id': job_id})

    flash('已开始导入任务，请稍候刷新查看结果。')
    return redirect(url_for('index'))


@app.route('/import_status/<job_id>')
def import_status(job_id):
    job = get_job(job_id)
    if not job:
        cleanup_expired_jobs()
        return jsonify({'success': False, 'message': '任务不存在'}), 404
    progress = 0
    if job['total_pages'] > 0:
        progress = round((job['processed_pages'] / job['total_pages']) * 100, 2)
    job_data = {
        'success': True,
        'job_id': job['job_id'],
        'status': job['status'],
        'current_page': job['current_page'],
        'processed_pages': job['processed_pages'],
        'total_pages': job['total_pages'],
        'imported_count': job['imported_count'],
        'skipped_count': job['skipped_count'],
        'status_message': job['status_message'],
        'error': job['error'],
        'progress': progress,
    }
    return jsonify(job_data)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8000, debug=False)
