from flask import Flask, render_template, request, session, redirect, jsonify, url_for
import os
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import secrets
import re
from dotenv import load_dotenv
import time

# loading the env variables
load_dotenv()

app = Flask(__name__)

# setting up the secret key and upload folder
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_urlsafe(32))
app.config['UPLOAD_FOLDER'] = 'uploads'
# make the folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# getting spotify keys from env file
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
SPOTIPY_REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')
SCOPE = 'playlist-modify-public playlist-modify-private'

# setting up tesseract path
tesseract_cmd = os.getenv('TESSERACT_CMD')
if tesseract_cmd:
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
elif os.name == 'nt':
    # if on windows, check default path
    default_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(default_path):
        pytesseract.pytesseract.tesseract_cmd = default_path

# regex patterns for cleaning text
RE_SPOTIFY_UI = re.compile(r'[►▶️⏸️⏯️⏭️⏮️⏹️🔀🔁•●◦⬇️💾📱]')
RE_DOTS = re.compile(r'\.{3,}|…')
RE_VIDEO = re.compile(r'\bVideo\b', re.IGNORECASE)
RE_TIMESTAMP = re.compile(r'\d{1,2}:\d{2}')
RE_WHITESPACE = re.compile(r'\s+')
RE_ARTIST_SEPARATORS = [' - ', ' – ', ' by ', ' | ', ' feat. ', ' feat ', ' ft. ', ' ft ', ' & ']

# function to make image better for reading
def preprocess_image(image_path):
    try:
        image = Image.open(image_path)
        
        # change to black and white
        image = image.convert('L')
        
        # make image bigger if it's too small
        width, height = image.size
        if width < 1000:
            scale_factor = 1000 / width
            new_width = int(width * scale_factor)
            new_height = int(height * scale_factor)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # make contrast better
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)
        
        # make it sharper
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(2.0)
        
        return image
    except Exception as e:
        print(f"Error preprocessing image: {e}")
        return None

# function to get text from image
def extract_text_optimized(image_path):
    image = preprocess_image(image_path)
    if not image:
        return ""
    
    # try to read text with tesseract
    try:
        text = pytesseract.image_to_string(image, config='--oem 3 --psm 6')
        
        # if text is too short, try again with default settings
        if len(text.strip()) < 10:
            text = pytesseract.image_to_string(image)
            
        return text
    except Exception as e:
        print(f"OCR Error: {e}")
        return ""

# function to clean up the text
def clean_spotify_text(text):
    if not text: return ""
    
    # remove weird symbols and extra spaces
    text = RE_SPOTIFY_UI.sub('', text)
    text = RE_DOTS.sub('', text)
    text = RE_VIDEO.sub('', text)
    text = RE_TIMESTAMP.sub('', text)
    text = RE_WHITESPACE.sub(' ', text).strip()
    
    return text

# check if text looks like a song
def is_likely_song_title(text):
    if not text or len(text) < 2:
        return False
    
    # ignore common words
    text_lower = text.lower()
    if text_lower in {'home', 'search', 'library', 'create', 'liked songs', 'add songs'}:
        return False
        
    # ignore if it's just numbers
    if re.match(r'^\d+$', text): 
        return False
        
    return True

# main function to find songs in text
def extract_songs_from_text(text):
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    songs = []
    
    i = 0
    while i < len(lines):
        line = clean_spotify_text(lines[i])
        
        if not is_likely_song_title(line):
            i += 1
            continue
            
        # check if artist is in the same line
        found_separator = False
        for sep in RE_ARTIST_SEPARATORS:
            if sep in line:
                parts = line.split(sep, 1)
                if len(parts) == 2:
                    song = parts[0].strip()
                    artist = parts[1].strip()
                    if len(song) > 1 and len(artist) > 1:
                        songs.append((song, artist))
                        found_separator = True
                        break
        
        if found_separator:
            i += 1
            continue
            
        # check if artist is in the next line
        if i + 1 < len(lines):
            next_line = clean_spotify_text(lines[i+1])
            if is_likely_song_title(next_line):
                songs.append((line, next_line))
                i += 2
                continue
                
        # if no artist found, just add the song
        if len(line) > 3:
            songs.append((line, ''))
            
        i += 1
        
    return songs

# search for song on spotify
def get_spotify_track(song, artist, sp):
    # remove special chars
    song_clean = re.sub(r'[^\w\s]', '', song).strip()
    artist_clean = re.sub(r'[^\w\s]', '', artist).strip() if artist else ''
    
    strategies = []
    
    if artist_clean:
        strategies.append(f'track:"{song_clean}" artist:"{artist_clean}"') # exact match
        strategies.append(f'{song_clean} {artist_clean}') # loose match
    
    strategies.append(f'track:"{song_clean}"') # just song name
    
    # try searching with different strategies
    for strategy in strategies:
        try:
            results = sp.search(q=strategy, type='track', limit=1, market='US')
            if results['tracks']['items']:
                return format_track_info(results['tracks']['items'][0])
        except Exception as e:
            print(f"Search error: {e}")
            continue
            
    return None

# helper to format track info
def format_track_info(track):
    return {
        'uri': track['uri'],
        'name': track['name'],
        'artist': ', '.join(a['name'] for a in track['artists']),
        'image': track['album']['images'][0]['url'] if track['album']['images'] else '',
        'id': track['id'],
        'preview_url': track.get('preview_url')
    }

# home page route
@app.route('/', methods=['GET', 'POST'])
def upload_image():
    if 'token_info' not in session:
        return render_template('upload.html')

    if request.method == 'POST':
        if 'images' not in request.files:
            return render_template('upload.html', error="No files part")
        
        files = request.files.getlist('images')
        if not files or all(f.filename == '' for f in files):
            return render_template('upload.html', error="No selected files")
        
        if len(files) > 10:
            return render_template('upload.html', error="Max 10 images allowed.")

        all_song_pairs = set() 
        filepaths = []
        
        try:
            # loop through uploaded files
            for file in files:
                if file and file.filename:
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                    file.save(filepath)
                    filepaths.append(filepath)
                    
                    # extract text and songs
                    text = extract_text_optimized(filepath)
                    pairs = extract_songs_from_text(text)
                    for p in pairs:
                        all_song_pairs.add(p)
            
            # search on spotify
            token_info = session.get('token_info')
            sp = spotipy.Spotify(auth=token_info['access_token'])
            
            found_songs = []
            not_found = []
            
            for song, artist in all_song_pairs:
                track_info = get_spotify_track(song, artist, sp)
                if track_info:
                    found_songs.append(track_info)
                else:
                    not_found.append((song, artist))
            
            # save uris in session
            session['track_uris'] = [s['uri'] for s in found_songs]
            
            # delete files after processing
            for filepath in filepaths:
                if os.path.exists(filepath):
                    os.remove(filepath)

            return render_template('songs.html',
                                   songs=found_songs,
                                   not_found=not_found,
                                   total_extracted=len(all_song_pairs))
                                   
        except Exception as e:
            # clean up if error
            for filepath in filepaths:
                if os.path.exists(filepath):
                    os.remove(filepath)
            print(f"Error: {e}")
            return render_template('upload.html', error="An error occurred during processing.")

    return render_template('upload.html')

# login route
@app.route('/login')
def login():
    if not SPOTIPY_CLIENT_ID or not SPOTIPY_CLIENT_SECRET or not SPOTIPY_REDIRECT_URI:
        return "Error: Spotify credentials not configured in .env", 500
        
    sp_oauth = SpotifyOAuth(
        SPOTIPY_CLIENT_ID,
        SPOTIPY_CLIENT_SECRET,
        SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)

# callback route after login
@app.route('/callback')
def callback():
    sp_oauth = SpotifyOAuth(
        SPOTIPY_CLIENT_ID,
        SPOTIPY_CLIENT_SECRET,
        SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )
    session.clear()
    code = request.args.get('code')
    try:
        token_info = sp_oauth.get_access_token(code)
        session['token_info'] = token_info
        return redirect('/')
    except Exception as e:
        return f"Authentication failed: {e}", 400

# route to add a single song
@app.route('/add_song', methods=['POST'])
def add_song():
    if 'token_info' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    track_uri = request.form.get('track_uri')
    if not track_uri:
        return jsonify({'success': False, 'message': 'No track URI'}), 400

    try:
        token_info = session.get('token_info')
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        playlist_id = get_or_create_playlist(sp)
        sp.playlist_add_items(playlist_id, [track_uri])
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# route to add all songs
@app.route('/add_all', methods=['POST'])
def add_all():
    if 'token_info' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    track_uris = session.get('track_uris')
    if not track_uris:
        return jsonify({'success': False, 'message': 'No songs to add'}), 400
    
    try:
        token_info = session.get('token_info')
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        playlist_id = get_or_create_playlist(sp)
        
        # add songs in batches of 100
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i+100]
            sp.playlist_add_items(playlist_id, batch)
        
        return jsonify({'success': True, 'count': len(track_uris)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# search route
@app.route('/search_songs', methods=['POST'])
def search_songs():
    if 'token_info' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    query = request.form.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'message': 'No query'}), 400

    try:
        token_info = session.get('token_info')
        sp = spotipy.Spotify(auth=token_info['access_token'])
        results = sp.search(q=query, type='track', limit=10, market='US')
        
        songs = [format_track_info(track) for track in results['tracks']['items']]
        return jsonify({'success': True, 'songs': songs})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# helper to get playlist id
def get_or_create_playlist(sp):
    playlist_id = session.get('playlist_id')
    if not playlist_id:
        user_id = sp.current_user()['id']
        import random, string
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        playlist_name = f'Extracted Playlist {suffix}'
        playlist = sp.user_playlist_create(user=user_id, name=playlist_name, public=False)
        playlist_id = playlist['id']
        session['playlist_id'] = playlist_id
    return playlist_id

if __name__ == '__main__':
    app.run(debug=True, port=5000)
