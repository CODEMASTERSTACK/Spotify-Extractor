from flask import Flask, render_template, request, session, redirect, jsonify
import os
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import secrets
import re

app = Flask(__name__)
app.secret_key = secrets.token_urlsafe(32)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Set tesseract_cmd path if needed (Windows)
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

SPOTIPY_CLIENT_ID = '7952a59e4b8c491c99abb40ea8945f39'
SPOTIPY_CLIENT_SECRET = '656897f8d40b42bd805addda8efc8b35'
SPOTIPY_REDIRECT_URI = 'https://ddc6-2401-4900-883f-4e39-fc15-14c7-e1fc-4384.ngrok-free.app/callback'
SCOPE = 'playlist-modify-public playlist-modify-private'

def preprocess_image(image_path):
    """Enhanced image preprocessing for better OCR results"""
    image = Image.open(image_path)
    
    # Convert to grayscale
    image = image.convert('L')
    
    # Resize image if too small (OCR works better on larger images)
    width, height = image.size
    if width < 1000:
        scale_factor = 1000 / width
        new_width = int(width * scale_factor)
        new_height = int(height * scale_factor)
        image = image.resize((new_width, new_height), Image.LANCZOS)
    
    # Enhance contrast
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2.0)
    
    # Enhance sharpness
    enhancer = ImageEnhance.Sharpness(image)
    image = enhancer.enhance(2.0)
    
    # Apply slight blur to reduce noise
    image = image.filter(ImageFilter.MedianFilter(size=3))
    
    return image

def extract_text_with_multiple_methods(image_path):
    """Try multiple OCR configurations to get best results"""
    image = preprocess_image(image_path)
    
    # Method 1: Default OCR
    text1 = pytesseract.image_to_string(image)
    
    # Method 2: OCR with specific config for mobile UI
    custom_config = r'--oem 3 --psm 6'
    text2 = pytesseract.image_to_string(image, config=custom_config)
    
    # Method 3: OCR optimized for vertical text blocks
    text3 = pytesseract.image_to_string(image, config='--psm 4')
    
    # Method 4: Single text block
    text4 = pytesseract.image_to_string(image, config='--psm 7')
    
    # Combine and return the best result (longest meaningful text)
    texts = [text1, text2, text3, text4]
    best_text = max(texts, key=lambda x: len([line for line in x.split('\n') if len(line.strip()) > 3]))
    
    print("=== OCR Results ===")
    print(f"Best text chosen (length: {len(best_text)}):")
    print(best_text)
    print("===================")
    
    return best_text

def clean_spotify_text(text):
    """Clean text specifically for Spotify mobile interface"""
    # Remove common Spotify UI elements
    text = re.sub(r'[►▶️⏸️⏯️⏭️⏮️⏹️🔀🔁]', '', text)
    text = re.sub(r'[•●◦]', '', text)
    text = re.sub(r'\.{3,}|…', '', text)
    
    # Remove "Video" indicators
    text = re.sub(r'\bVideo\b', '', text, flags=re.IGNORECASE)
    
    # Remove timestamps/durations
    text = re.sub(r'\d{1,2}:\d{2}', '', text)
    
    # Remove download/offline indicators
    text = re.sub(r'[⬇️💾📱]', '', text)
    
    # Clean up extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def is_likely_song_title(text):
    """Determine if text is likely a song title"""
    if not text or len(text.strip()) < 2:
        return False
    
    # Remove common non-song indicators
    exclude_patterns = [
        r'^\d+$',  # Just numbers
        r'^\d{1,2}:\d{2}$',  # Time format
        r'^[►▶️⏸️⏯️⏭️⏮️⏹️]+$',  # Just player buttons
        r'^[•●◦]+$',  # Just bullets
        r'^(Home|Search|Library|Create)$',  # Navigation
        r'^(Video|Audio|Playlist)$',  # Media types
        r'^(Add|Remove|Download|Share)$',  # Actions
        r'^(Pixie|Wade|Your Library)$',  # Specific to your playlist
    ]
    
    for pattern in exclude_patterns:
        if re.match(pattern, text.strip(), re.IGNORECASE):
            return False
    
    return True

def extract_songs_from_spotify_text(text):
    """Extract songs from Spotify mobile interface text"""
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    # Clean each line
    cleaned_lines = []
    for line in lines:
        cleaned = clean_spotify_text(line)
        if is_likely_song_title(cleaned):
            cleaned_lines.append(cleaned)
    
    print("=== Cleaned Lines ===")
    for i, line in enumerate(cleaned_lines):
        print(f"{i}: '{line}'")
    print("=====================")
    
    songs = []
    i = 0
    
    while i < len(cleaned_lines):
        current_line = cleaned_lines[i]
        
        # Skip obviously non-song lines
        if not is_likely_song_title(current_line):
            i += 1
            continue
        
        # Method 1: Check for separators in single line (Song - Artist)
        separators = [' - ', ' – ', ' by ', ' | ', ' feat. ', ' feat ', ' ft. ', ' ft ', ' & ']
        found_separator = False
        
        for sep in separators:
            if sep in current_line:
                parts = current_line.split(sep, 1)
                if len(parts) == 2:
                    song = parts[0].strip()
                    artist = parts[1].strip()
                    
                    # Clean up common artifacts
                    artist = re.sub(r'^(feat\.|ft\.|featuring|with)\s*', '', artist, flags=re.IGNORECASE)
                    
                    if song and artist and len(song) > 1 and len(artist) > 1:
                        songs.append((song, artist))
                        found_separator = True
                        break
        
        if found_separator:
            i += 1
            continue
        
        # Method 2: Look for pattern where next line might be artist
        if i + 1 < len(cleaned_lines):
            next_line = cleaned_lines[i + 1]
            
            # Check if next line looks like an artist name
            # Artists are usually shorter and don't contain certain words
            if (len(next_line) < len(current_line) * 1.5 and 
                len(next_line) > 2 and
                not re.search(r'\b(the|and|of|in|to|a|an|with|for|on|at|by)\b', next_line.lower()) and
                not re.match(r'^\d+', next_line)):
                
                songs.append((current_line, next_line))
                i += 2
                continue
        
        # Method 3: Treat as song with unknown artist
        if len(current_line) > 3:
            songs.append((current_line, ''))
        
        i += 1
    
    print("=== Extracted Songs ===")
    for i, (song, artist) in enumerate(songs):
        print(f"{i}: '{song}' by '{artist}'")
    print("=======================")
    
    return songs

def calculate_similarity(str1, str2):
    """Simple similarity calculation"""
    if not str1 or not str2:
        return 0
    
    # Convert to sets of words
    words1 = set(str1.lower().split())
    words2 = set(str2.lower().split())
    
    if not words1 or not words2:
        return 0
    
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    
    return len(intersection) / len(union)

def get_spotify_track(song, artist, sp):
    """Enhanced Spotify search with better matching"""
    
    # Clean search terms
    song_clean = re.sub(r'[^\w\s]', '', song).strip()
    artist_clean = re.sub(r'[^\w\s]', '', artist).strip() if artist else ''
    
    search_strategies = []
    
    # Strategy 1: Exact match with quotes
    if artist_clean:
        search_strategies.append(f'track:"{song_clean}" artist:"{artist_clean}"')
    
    # Strategy 2: Without quotes but with track/artist prefixes
    if artist_clean:
        search_strategies.append(f'track:{song_clean} artist:{artist_clean}')
    
    # Strategy 3: Simple combination
    if artist_clean:
        search_strategies.append(f'{song_clean} {artist_clean}')
    
    # Strategy 4: Just song name with track prefix
    search_strategies.append(f'track:{song_clean}')
    
    # Strategy 5: Just song name
    search_strategies.append(song_clean)
    
    # Strategy 6: Try partial matches for long titles
    if len(song_clean) > 20:
        words = song_clean.split()
        if len(words) > 3:
            partial_song = ' '.join(words[:3])
            search_strategies.append(f'track:{partial_song}')
    
    for strategy in search_strategies:
        try:
            print(f"Trying search strategy: '{strategy}'")
            results = sp.search(q=strategy, type='track', limit=10, market='US')
            
            if results['tracks']['items']:
                # Find best match
                for track in results['tracks']['items']:
                    track_name = track['name'].lower()
                    track_artists = [a['name'].lower() for a in track['artists']]
                    
                    # Check if this is a good match
                    song_lower = song_clean.lower()
                    artist_lower = artist_clean.lower()
                    
                    name_match = (song_lower in track_name or 
                                track_name in song_lower or
                                calculate_similarity(song_lower, track_name) > 0.7)
                    
                    artist_match = (not artist_lower or 
                                  any(artist_lower in ta or ta in artist_lower for ta in track_artists))
                    
                    if name_match and artist_match:
                        print(f"Found match: {track['name']} by {track['artists'][0]['name']}")
                        return format_track_info(track)
                
                # If no perfect match, return first result for exact strategies
                if 'track:' in strategy and '"' in strategy:
                    print(f"Using first result: {results['tracks']['items'][0]['name']}")
                    return format_track_info(results['tracks']['items'][0])
        
        except Exception as e:
            print(f"Search error with strategy '{strategy}': {e}")
            continue
    
    print(f"No match found for: '{song}' by '{artist}'")
    return None

def format_track_info(track):
    """Format track information for display"""
    return {
        'uri': track['uri'],
        'name': track['name'],
        'artist': ', '.join(a['name'] for a in track['artists']),
        'image': track['album']['images'][0]['url'] if track['album']['images'] else '',
        'id': track['id'],
        'preview_url': track.get('preview_url')  # Add this line
    }

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
            return render_template('upload.html', error="You can upload a maximum of 10 images at once.")

        all_song_pairs = []
        filepaths = []
        try:
            # Save and process each image
            for file in files:
                if file and file.filename:
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                    file.save(filepath)
                    filepaths.append(filepath)
                    text = extract_text_with_multiple_methods(filepath)
                    song_pairs = extract_songs_from_spotify_text(text)
                    all_song_pairs.extend(song_pairs)
            
            # Remove duplicates
            all_song_pairs = list({(s, a) for s, a in all_song_pairs})

            # Search for tracks on Spotify
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

            # Store songs in session
            session['songs'] = found_songs
            session['not_found'] = not_found

            # Clean up uploaded files
            for filepath in filepaths:
                os.remove(filepath)

            return render_template('songs.html',
                                   songs=found_songs,
                                   not_found=not_found,
                                   total_extracted=len(all_song_pairs))
        except Exception as e:
            # Clean up in case of error
            for filepath in filepaths:
                if os.path.exists(filepath):
                    os.remove(filepath)
            return f"Error processing images: {str(e)}", 500

    return render_template('upload.html')

@app.route('/login')
def login():
    sp_oauth = SpotifyOAuth(
        SPOTIPY_CLIENT_ID,
        SPOTIPY_CLIENT_SECRET,
        SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)

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
    token_info = sp_oauth.get_access_token(code)
    session['token_info'] = token_info
    return redirect('/')

@app.route('/add_song', methods=['POST'])
def add_song():
    if 'token_info' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    track_uri = request.form.get('track_uri')
    track_name = request.form.get('track_name')
    if not track_uri:
        return jsonify({'success': False, 'message': 'No track URI'}), 400

    try:
        token_info = session.get('token_info')
        sp = spotipy.Spotify(auth=token_info['access_token'])

        # Create playlist if not already created
        playlist_id = session.get('playlist_id')
        if not playlist_id:
            user_id = sp.current_user()['id']
            import random, string
            playlist_name = 'Extracted Playlist ' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            playlist = sp.user_playlist_create(user=user_id, name=playlist_name, public=False)
            playlist_id = playlist['id']
            session['playlist_id'] = playlist_id

        # Add track to playlist
        sp.playlist_add_items(playlist_id, [track_uri])
        return jsonify({'success': True, 'message': f'Added {track_name} to playlist'})
    
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/add_all', methods=['POST'])
def add_all():
    """Add all found songs to playlist at once"""
    if 'token_info' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    if 'songs' not in session:
        return jsonify({'success': False, 'message': 'No songs to add'}), 400
    
    try:
        token_info = session.get('token_info')
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        # Create playlist if not already created
        playlist_id = session.get('playlist_id')
        if not playlist_id:
            user_id = sp.current_user()['id']
            import random, string
            playlist_name = 'Extracted Playlist ' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            playlist = sp.user_playlist_create(user=user_id, name=playlist_name, public=False)
            playlist_id = playlist['id']
            session['playlist_id'] = playlist_id
        
        # Add all tracks
        track_uris = [song['uri'] for song in session['songs']]
        sp.playlist_add_items(playlist_id, track_uris)
        
        return jsonify({'success': True, 'message': f'Added {len(track_uris)} songs to playlist'})
    
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/search_songs', methods=['POST'])
def search_songs():
    if 'token_info' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    query = request.form.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'message': 'No query provided'}), 400

    try:
        token_info = session.get('token_info')
        sp = spotipy.Spotify(auth=token_info['access_token'])
        results = sp.search(q=query, type='track', limit=10, market='US')
        songs = []
        for track in results['tracks']['items']:
            print(f"{track['name']} preview_url: {track.get('preview_url')}")
            songs.append({
                'uri': track['uri'],
                'name': track['name'],
                'artist': ', '.join(a['name'] for a in track['artists']),
                'image': track['album']['images'][0]['url'] if track['album']['images'] else '',
                'id': track['id'],
                'preview_url': track.get('preview_url')
            })
        return jsonify({'success': True, 'songs': songs})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)