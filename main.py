from fastapi import FastAPI, Request, Form, File, UploadFile, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import sqlite3
from datetime import datetime, timedelta, timezone
import os
import re
import requests
import threading
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlparse

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
SECRET_KEY = os.environ.get('SECRET_KEY', 'your_secret_key')


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title='message Display', lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount('/static', StaticFiles(directory=os.path.join(BASE_DIR, 'static')), name='static')

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, 'templates'))


def normalize_message_text(value):
    from bs4 import BeautifulSoup

    raw_html = (value or '').replace('\x00', '')
    text = BeautifulSoup(raw_html, 'html.parser').get_text('\n')
    text = text.replace('\xa0', ' ').replace('\r', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def display_text_filter(value):
    text = normalize_message_text(value)
    return text.replace('答：', '\n答：')


templates.env.filters['display_text'] = display_text_filter

IMG_BASE_URL = 'https://oneos.20110814.org/'
VID_BASE_URL = 'https://oneos.20110814.org/'


def secure_filename(filename):
    filename = os.path.basename(filename or '')
    filename = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
    return filename or ''


def row_to_dict(row):
    return dict(row) if row is not None else None


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


ROUTE_PATHS = {
    'index': '/',
    'search': '/search',
    'add_tweet': '/add',
    'import_data': '/import',
    'export_messages': '/export',
    'edit_tweet': '/edit/{tweet_id}',
    'batch_delete': '/batch_delete',
    'delete_tweet': '/delete/{tweet_id}',
}

ROUTE_PATH_PARAMS = {
    'edit_tweet': {'tweet_id'},
    'delete_tweet': {'tweet_id'},
}


def build_url_for(request: Request):
    def url_for(name: str, **values):
        values = {key: value for key, value in values.items() if value is not None}
        path_template = ROUTE_PATHS.get(name)
        if not path_template:
            return str(request.url_for(name))

        path_param_names = ROUTE_PATH_PARAMS.get(name, set())
        path_values = {key: values.pop(key) for key in list(values) if key in path_param_names}
        path = path_template.format(**path_values) if path_values else path_template
        if values:
            path = f'{path}?{urlencode(values)}'
        return path

    return url_for


def flash(request: Request, message: str):
    request.session.setdefault('_flashes', []).append(message)


def pop_flashes(request: Request):
    messages = request.session.get('_flashes', [])
    request.session['_flashes'] = []
    return messages


def render(request: Request, name: str, endpoint: str, **context):
    return templates.TemplateResponse(
        request,
        name,
        {
            'endpoint': endpoint,
            'url_for': build_url_for(request),
            'messages': pop_flashes(request),
            **context,
        },
    )


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


templates.env.filters['utc8'] = format_utc8


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


def get_return_url(request: Request, *, next_url: str = '', default_path: str = '/'):
    next_url = (next_url or request.query_params.get('next') or '').strip()
    if next_url.startswith('/'):
        return next_url

    referrer = (request.headers.get('referer') or '').strip()
    if referrer:
        parsed = urlparse(referrer)
        current_host = urlparse(str(request.base_url)).netloc
        if not parsed.netloc or parsed.netloc == current_host:
            path = parsed.path or '/'
            if parsed.query:
                path = f'{path}?{parsed.query}'
            return path

    return default_path


def build_tweet_form_data(source=None):
    source = source or {}
    return {
        'user': (source.get('user') or '').strip(),
        'text': source.get('text') or '',
        'created_at': (source.get('created_at') or '').strip(),
        'image_urls': source.get('image_urls') or '',
        'video_urls': source.get('video_urls') or '',
    }


async def save_upload_file(upload: UploadFile, filepath: str):
    content = await upload.read()
    with open(filepath, 'wb') as handle:
        handle.write(content)


async def collect_media_inputs(
    tweet_id,
    image_paths=None,
    video_paths=None,
    *,
    images=None,
    videos=None,
    image_urls_text='',
    video_urls_text='',
):
    ensure_directory(MEDIA_DIR)
    image_paths = list(image_paths or [])
    video_paths = list(video_paths or [])

    for img in images or []:
        if img and img.filename:
            filename = build_upload_filename(tweet_id, img.filename)
            filepath = os.path.join(MEDIA_DIR, filename)
            await save_upload_file(img, filepath)
            image_paths.append(filename)

    for vid in videos or []:
        if vid and vid.filename:
            filename = build_upload_filename(tweet_id, vid.filename)
            filepath = os.path.join(MEDIA_DIR, filename)
            await save_upload_file(vid, filepath)
            video_paths.append(filename)

    for url in image_urls_text.strip().split('\n'):
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
                with open(filepath, 'wb') as handle:
                    handle.write(resp.content)
                image_paths.append(filename)
        except Exception:
            pass

    for url in video_urls_text.strip().split('\n'):
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
                with open(filepath, 'wb') as handle:
                    handle.write(resp.content)
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
    for statement in (
        'ALTER TABLE tweets ADD COLUMN image_paths TEXT DEFAULT ""',
        'ALTER TABLE tweets ADD COLUMN video_paths TEXT DEFAULT ""',
        'ALTER TABLE import_jobs ADD COLUMN url TEXT DEFAULT "https://20110814.org/api/gengxin?type=all&page={page}"',
        'ALTER TABLE import_jobs ADD COLUMN img_base_url TEXT DEFAULT "https://oneos.20110814.org/"',
        'ALTER TABLE import_jobs ADD COLUMN vid_base_url TEXT DEFAULT "https://oneos.20110814.org/"',
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
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
        pass
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
    for verify_ssl in [True, False]:
        try:
            resp = requests.get(media_url, timeout=10, verify=verify_ssl)
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'wb') as handle:
                    handle.write(resp.content)
                return True
        except Exception:
            continue
    return False


def perform_import(start_page, end_page, job_id=None):
    from bs4 import BeautifulSoup

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

        with ThreadPoolExecutor(max_workers=5) as executor:
            for index, page in enumerate(pages, start=1):
                if job_id:
                    update_job(job_id, status='running', current_page=page, processed_pages=index - 1)
                url = base_url.format(page=page)
                try:
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

                post_images = {i: [] for i in range(len(posts))}
                post_videos = {i: [] for i in range(len(posts))}
                download_tasks = []
                media_info = []

                for post_idx, post in enumerate(posts):
                    def normalize_media_filename(url_or_name):
                        if not url_or_name:
                            return None
                        name = url_or_name.split('?')[0].split('#')[0]
                        name = name.replace('..', '').strip('/')
                        return name or None

                    user_name_full = post.get('user_name', '').replace('\x00', '')
                    user = user_name_full.split(' @')[0] if ' @' in user_name_full else user_name_full
                    raw_text = post.get('text_show', '').replace('\x00', '')
                    text = build_import_text(raw_text)
                    created_at = normalize_created_at_for_storage(post.get('created_s', ''))

                    pic_ids_str = (post.get('pic_ids') or '').strip()
                    if pic_ids_str:
                        raw_filenames = [f.strip() for f in pic_ids_str.split(',') if f.strip()]
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

                for task in as_completed(download_tasks):
                    success = task.result()
                    for info in media_info:
                        if info['task'] == task:
                            if success:
                                if info['type'] == 'image':
                                    post_images[info['post_idx']].append(info['filename'])
                                elif info['type'] == 'video':
                                    post_videos[info['post_idx']].append(info['filename'])
                            break

                for post_idx, post in enumerate(posts):
                    user_name_full = post.get('user_name', '').replace('\x00', '')
                    user = user_name_full.split(' @')[0] if ' @' in user_name_full else user_name_full
                    text = build_import_text(post.get('text_show', ''))
                    created_at = normalize_created_at_for_storage(post.get('created_s', ''))

                    image_paths_str = ','.join(post_images[post_idx])
                    video_paths_str = ','.join(post_videos[post_idx])

                    if user and created_at and (text or image_paths_str or video_paths_str):
                        cursor = conn.execute(
                            'SELECT tweet_id FROM tweets WHERE user = ? AND created_at = ? AND text = ?',
                            (user, created_at, text)
                        )
                        existing = cursor.fetchone()
                        if existing:
                            conn.execute(
                                'UPDATE tweets SET image_paths = ?, video_paths = ? WHERE tweet_id = ?',
                                (image_paths_str, video_paths_str, existing['tweet_id'])
                            )
                            skipped_count += 1
                        else:
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


def should_return_json(request: Request):
    requested_with = request.headers.get('X-Requested-With', '').lower()
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept or requested_with == 'xmlhttprequest'



@app.get('/', response_class=HTMLResponse, name='index')
def index(
    request: Request,
    page: int = Query(1, ge=1),
    onlymedia: str = Query(''),
):
    onlymedia_flag = onlymedia.strip() in ('1', 'true', 'on')
    per_page = 20
    offset = (page - 1) * per_page

    where_sql = ''
    params = []
    if onlymedia_flag:
        where_sql = "WHERE (COALESCE(image_paths, '') != '' OR COALESCE(video_paths, '') != '')"

    conn = get_db_connection()
    tweets = conn.execute(
        f'SELECT * FROM tweets {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?',
        [*params, per_page, offset]
    ).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM tweets {where_sql}', params).fetchone()[0]
    conn.close()

    total_pages = (total + per_page - 1) // per_page
    return render(
        request,
        'index.html',
        'index',
        tweets=rows_to_dicts(tweets),
        page=page,
        total_pages=total_pages,
        total=total,
        onlymedia=onlymedia_flag,
    )


@app.get('/search', response_class=HTMLResponse, name='search')
def search(
    request: Request,
    page: int = Query(1, ge=1),
    query: str = Query(''),
    start_date: str = Query(''),
    end_date: str = Query(''),
    onlymedia: str = Query(''),
):
    onlymedia_flag = onlymedia.strip() in ('1', 'true', 'on')
    has_filters = any([query.strip(), start_date.strip(), end_date.strip(), onlymedia_flag])

    if not has_filters:
        return render(
            request,
            'search.html',
            'search',
            query=query,
            start_date=start_date,
            end_date=end_date,
            onlymedia=onlymedia_flag,
            has_filters=False,
            result_count=0,
            page=1,
            total_pages=0,
        )

    page = max(page, 1)
    offset = (page - 1) * SEARCH_PER_PAGE
    where_clauses = ['1=1']
    params = []

    if query.strip():
        where_clauses.append('(user LIKE ? OR text LIKE ?)')
        params.extend([f'%{query.strip()}%', f'%{query.strip()}%'])

    if start_date.strip():
        where_clauses.append('created_at >= ?')
        params.append(utc8_date_to_storage_boundary(start_date.strip()))

    if end_date.strip():
        where_clauses.append('created_at <= ?')
        params.append(utc8_date_to_storage_boundary(end_date.strip(), end_of_day=True))

    if onlymedia_flag:
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
    return render(
        request,
        'search.html',
        'search',
        tweets=rows_to_dicts(tweets),
        query=query,
        start_date=start_date,
        end_date=end_date,
        onlymedia=onlymedia_flag,
        result_count=total,
        has_filters=True,
        page=page,
        total_pages=total_pages,
    )


@app.post('/search', name='search_post')
async def search_post(
    request: Request,
    query: str = Form(''),
    start_date: str = Form(''),
    end_date: str = Form(''),
    onlymedia: str = Form(''),
):
    params = {
        'query': query.strip(),
        'start_date': start_date.strip(),
        'end_date': end_date.strip(),
        'page': 1,
    }
    if onlymedia:
        params['onlymedia'] = '1'
    return RedirectResponse(str(request.url_for('search', **params)), status_code=303)


@app.get('/import', response_class=HTMLResponse, name='import_data')
def import_data(request: Request):
    return render(request, 'import.html', 'import_data')


@app.get('/export', response_class=HTMLResponse, name='export_messages')
def export_messages_get(request: Request):
    return render(
        request,
        'export.html',
        'export_messages',
        export_result=None,
        start_date='',
        end_date='',
        export_name='',
    )


@app.post('/export', response_class=HTMLResponse)
async def export_messages_post(
    request: Request,
    start_date: str = Form(''),
    end_date: str = Form(''),
    export_name: str = Form(''),
):
    export_result = None
    start_date = start_date.strip()
    end_date = end_date.strip()
    export_name = export_name.strip()

    if not start_date or not end_date:
        flash(request, '请填写开始日期和结束日期。')
    elif start_date > end_date:
        flash(request, '开始日期不能晚于结束日期。')
    else:
        export_result = export_messages_to_markdown(start_date, end_date, export_name)
        if export_result is None:
            flash(request, '该时间范围内没有可导出的消息。')
        else:
            flash(request, '导出完成。')

    return render(
        request,
        'export.html',
        'export_messages',
        export_result=export_result,
        start_date=start_date,
        end_date=end_date,
        export_name=export_name,
    )


@app.get('/add', response_class=HTMLResponse, name='add_tweet')
def add_tweet_get(request: Request):
    form_data = build_tweet_form_data({'created_at': now_utc8_text()})
    return render(request, 'add.html', 'add_tweet', form_data=form_data)


@app.post('/add', response_class=HTMLResponse)
async def add_tweet_post(
    request: Request,
    user: str = Form(''),
    text: str = Form(''),
    created_at: str = Form(''),
    image_urls: str = Form(''),
    video_urls: str = Form(''),
    images: list[UploadFile] = File(default=[]),
    videos: list[UploadFile] = File(default=[]),
):
    form_data = build_tweet_form_data({
        'user': user,
        'text': text,
        'created_at': created_at,
        'image_urls': image_urls,
        'video_urls': video_urls,
    })
    cleaned_user = form_data['user']
    cleaned_text = clean_message_text(form_data['text'])
    stored_created_at = utc8_input_to_storage(form_data['created_at'])

    if not cleaned_user or not cleaned_text or not stored_created_at:
        flash(request, '请填写用户、内容和创建时间。')
        return render(request, 'add.html', 'add_tweet', form_data=form_data)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            'INSERT INTO tweets (user, text, created_at, image_paths, video_paths) VALUES (?, ?, ?, ?, ?)',
            (cleaned_user, cleaned_text, stored_created_at, '', '')
        )
        tweet_id = cursor.lastrowid
        image_paths, video_paths = await collect_media_inputs(
            tweet_id,
            images=images,
            videos=videos,
            image_urls_text=form_data['image_urls'],
            video_urls_text=form_data['video_urls'],
        )
        conn.execute(
            'UPDATE tweets SET image_paths = ?, video_paths = ? WHERE tweet_id = ?',
            (','.join(image_paths), ','.join(video_paths), tweet_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash(request, '已存在相同用户、时间和内容的消息。')
        return render(request, 'add.html', 'add_tweet', form_data=form_data)
    finally:
        conn.close()

    flash(request, '消息已创建')
    return RedirectResponse(str(request.url_for('index')), status_code=303)


@app.get('/edit/{tweet_id}', response_class=HTMLResponse, name='edit_tweet')
def edit_tweet_get(request: Request, tweet_id: int):
    conn = get_db_connection()
    tweet = conn.execute('SELECT * FROM tweets WHERE tweet_id = ?', (tweet_id,)).fetchone()
    conn.close()
    if not tweet:
        flash(request, '消息不存在')
        return RedirectResponse(str(request.url_for('index')), status_code=303)
    return render(request, 'edit.html', 'edit_tweet', tweet=row_to_dict(tweet))


@app.post('/edit/{tweet_id}', response_class=HTMLResponse)
async def edit_tweet_post(
    request: Request,
    tweet_id: int,
    user: str = Form(...),
    text: str = Form(...),
    created_at: str = Form(...),
    image_urls: str = Form(''),
    video_urls: str = Form(''),
    images: list[UploadFile] = File(default=[]),
    videos: list[UploadFile] = File(default=[]),
):
    conn = get_db_connection()
    tweet = conn.execute('SELECT * FROM tweets WHERE tweet_id = ?', (tweet_id,)).fetchone()
    if not tweet:
        conn.close()
        flash(request, '消息不存在')
        return RedirectResponse(str(request.url_for('index')), status_code=303)

    cleaned_text = clean_message_text(text)
    stored_created_at = utc8_input_to_storage(created_at)
    image_paths, video_paths = await collect_media_inputs(
        tweet_id,
        split_media_paths(tweet['image_paths']),
        split_media_paths(tweet['video_paths']),
        images=images,
        videos=videos,
        image_urls_text=image_urls,
        video_urls_text=video_urls,
    )
    conn.execute(
        'UPDATE tweets SET user = ?, text = ?, created_at = ?, image_paths = ?, video_paths = ? WHERE tweet_id = ?',
        (user, cleaned_text, stored_created_at, ','.join(image_paths), ','.join(video_paths), tweet_id)
    )
    conn.commit()
    conn.close()
    flash(request, '消息已更新')
    return RedirectResponse(str(request.url_for('index')), status_code=303)


@app.post('/delete/{tweet_id}', name='delete_tweet')
async def delete_tweet(request: Request, tweet_id: int, next: str = Form('')):
    conn = get_db_connection()
    conn.execute('DELETE FROM tweets WHERE tweet_id = ?', (tweet_id,))
    conn.commit()
    conn.close()
    flash(request, '消息已删除')
    return RedirectResponse(get_return_url(request, next_url=next), status_code=303)


@app.post('/batch_delete', name='batch_delete')
async def batch_delete(request: Request, selected: list[str] = Form(default=[]), next: str = Form('')):
    if selected:
        conn = get_db_connection()
        deleted_count = 0
        for tweet_id in selected:
            cursor = conn.execute('DELETE FROM tweets WHERE tweet_id = ?', (tweet_id,))
            deleted_count += cursor.rowcount
        conn.commit()
        conn.close()
        flash(request, f'已删除 {deleted_count} 条消息')
    else:
        flash(request, '未选择任何消息')
    return RedirectResponse(get_return_url(request, next_url=next), status_code=303)


@app.post('/clear_database', name='clear_database')
async def clear_database(request: Request):
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

    flash(request, '数据库内容和媒体文件已全部清空。')
    return RedirectResponse(str(request.url_for('index')), status_code=303)


@app.post('/import_web', name='import_web')
async def import_web(
    request: Request,
    start_page: str = Form('1'),
    end_page: str = Form('10'),
    url: str = Form('https://20110814.org/api/gengxin?type=all&page={page}'),
    img_base_url: str = Form('https://oneos.20110814.org/'),
    vid_base_url: str = Form('https://oneos.20110814.org/'),
):
    try:
        start_page_int = int(start_page)
        end_page_int = int(end_page)
    except (TypeError, ValueError):
        message = '页码格式不正确。'
        if should_return_json(request):
            return JSONResponse({'success': False, 'message': message}, status_code=400)
        flash(request, message)
        return RedirectResponse(str(request.url_for('import_data')), status_code=303)

    if start_page_int > end_page_int or start_page_int < 1:
        message = '页码范围无效，确保起始页和结束页都大于等于 1，且起始页不晚于结束页。'
        if should_return_json(request):
            return JSONResponse({'success': False, 'message': message}, status_code=400)
        flash(request, message)
        return RedirectResponse(str(request.url_for('import_data')), status_code=303)

    job_id = create_import_job(start_page_int, end_page_int, url, img_base_url, vid_base_url)
    thread = threading.Thread(target=run_import_job, args=(job_id, start_page_int, end_page_int), daemon=True)
    thread.start()

    if should_return_json(request):
        return JSONResponse({'success': True, 'job_id': job_id})

    flash(request, '已开始导入任务，请稍候刷新查看结果。')
    return RedirectResponse(str(request.url_for('index')), status_code=303)


@app.get('/import_status/{job_id}', name='import_status')
def import_status(job_id: str):
    job = get_job(job_id)
    if not job:
        cleanup_expired_jobs()
        return JSONResponse({'success': False, 'message': '任务不存在'}, status_code=404)
    progress = 0
    if job['total_pages'] > 0:
        progress = round((job['processed_pages'] / job['total_pages']) * 100, 2)
    return JSONResponse({
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
    })


if __name__ == '__main__':
    import uvicorn

    init_db()
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=False)
