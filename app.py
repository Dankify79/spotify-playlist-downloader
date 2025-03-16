# app.py
from flask import Flask, render_template, request, send_file, jsonify, url_for
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL
import eyed3
import requests
import os
import uuid
import shutil
import threading
import re
import time
import zipfile
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app)

# Configure these with your Spotify API credentials
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')

# Store active download sessions
active_downloads = {}

def setup_spotify_client():
    client_credentials_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID, 
        client_secret=SPOTIFY_CLIENT_SECRET
    )
    return spotipy.Spotify(client_credentials_manager=client_credentials_manager)

def get_playlist_tracks(sp, playlist_url):
    try:
        playlist_id = playlist_url.split('/')[-1].split('?')[0]
        results = sp.playlist_tracks(playlist_id)
        
        tracks = []
        for item in results['items']:
            track = item['track']
            if track:  # Some items might be None
                tracks.append({
                    'name': track['name'],
                    'artist': track['artists'][0]['name'],
                    'album': track['album']['name'],
                    'release_date': track['album']['release_date'] if 'release_date' in track['album'] else None,
                    'image_url': track['album']['images'][0]['url'] if track['album']['images'] else None
                })
        
        return {'success': True, 'tracks': tracks}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def download_song(track_info, output_dir, session_id):
    query = f"{track_info['artist']} - {track_info['name']}"
    filename = f"{track_info['artist']} - {track_info['name']}"
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    
    output_path = os.path.join(output_dir, f"{filename}.mp3")
    
    # Update progress
    socketio.emit('download_progress', {
        'session_id': session_id,
        'status': 'downloading',
        'current_track': query
    })
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'outtmpl': output_path[:-4],
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch',
        'noplaylist': True,
    }
    
    with YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([f"ytsearch:{query}"])
            return output_path
        except Exception as e:
            socketio.emit('download_progress', {
                'session_id': session_id,
                'status': 'error',
                'message': f"Error downloading {query}: {str(e)}"
            })
            return None

def download_cover_art(image_url, output_path):
    if not image_url:
        return None
        
    response = requests.get(image_url, stream=True)
    if response.status_code == 200:
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)
        return output_path
    return None

def set_metadata(mp3_path, track_info, cover_art_path=None):
    try:
        audiofile = eyed3.load(mp3_path)
        
        if not audiofile or not audiofile.tag:
            audiofile = eyed3.load(mp3_path, filetype=eyed3.id3.ID3_V2_3)
            if not audiofile:
                audiofile = eyed3.load(mp3_path, filetype=eyed3.id3.ID3_V2_4)
            if not audiofile.tag:
                audiofile.initTag()
                
        audiofile.tag.title = track_info['name']
        audiofile.tag.artist = track_info['artist']
        audiofile.tag.album = track_info['album']
        
        if 'release_date' in track_info and track_info['release_date']:
            try:
                year = int(track_info['release_date'].split('-')[0])
                audiofile.tag.release_date = year
            except:
                pass
                
        if cover_art_path:
            with open(cover_art_path, 'rb') as cover_art:
                audiofile.tag.images.set(3, cover_art.read(), 'image/jpeg')
                
        audiofile.tag.save()
        return True
    except Exception as e:
        return False

def create_zip_file(session_dir, zip_path):
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, dirs, files in os.walk(session_dir):
            for file in files:
                if file.endswith('.mp3'):
                    zipf.write(os.path.join(root, file), 
                              os.path.relpath(os.path.join(root, file), 
                                             os.path.join(session_dir, '..')))

def download_playlist_task(playlist_url, session_id):
    session_dir = os.path.join('downloads', session_id)
    if not os.path.exists(session_dir):
        os.makedirs(session_dir)
        
    sp = setup_spotify_client()
    result = get_playlist_tracks(sp, playlist_url)
    
    if not result['success']:
        socketio.emit('download_progress', {
            'session_id': session_id,
            'status': 'error',
            'message': f"Error fetching playlist: {result['error']}"
        })
        active_downloads[session_id]['status'] = 'error'
        return
    
    tracks = result['tracks']
    total_tracks = len(tracks)
    
    socketio.emit('download_progress', {
        'session_id': session_id,
        'status': 'started',
        'total_tracks': total_tracks
    })
    
    completed_tracks = 0
    
    for i, track in enumerate(tracks):
        mp3_path = download_song(track, session_dir, session_id)
        if not mp3_path:
            continue
            
        cover_art_path = None
        if track['image_url']:
            cover_art_path = os.path.join(session_dir, f"temp_cover_{i}.jpg")
            download_cover_art(track['image_url'], cover_art_path)
            
        set_metadata(mp3_path, track, cover_art_path)
        
        if cover_art_path and os.path.exists(cover_art_path):
            os.remove(cover_art_path)
            
        completed_tracks += 1
        
        # Update progress
        socketio.emit('download_progress', {
            'session_id': session_id,
            'status': 'progress',
            'completed': completed_tracks,
            'total': total_tracks,
            'percentage': int((completed_tracks / total_tracks) * 100)
        })
    
    # Create a zip file of all downloads
    zip_path = os.path.join('downloads', f"{session_id}.zip")
    create_zip_file(session_dir, zip_path)
    
    # Update status
    active_downloads[session_id]['status'] = 'complete'
    active_downloads[session_id]['zip_path'] = zip_path
    
    socketio.emit('download_progress', {
        'session_id': session_id,
        'status': 'complete',
        'download_url': url_for('download_file', session_id=session_id)
    })
    
    # Schedule cleanup
    def cleanup():
        time.sleep(3600)  # Keep files for 1 hour
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if session_id in active_downloads:
            del active_downloads[session_id]
            
    cleanup_thread = threading.Thread(target=cleanup)
    cleanup_thread.daemon = True
    cleanup_thread.start()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/start_download', methods=['POST'])
def start_download():
    playlist_url = request.form.get('playlist_url')
    
    if not playlist_url:
        return jsonify({'success': False, 'error': 'No playlist URL provided'})
    
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return jsonify({'success': False, 'error': 'Spotify API credentials not configured'})
    
    session_id = str(uuid.uuid4())
    active_downloads[session_id] = {
        'status': 'starting',
        'playlist_url': playlist_url,
        'start_time': time.time()
    }
    
    # Start download in background
    download_thread = threading.Thread(
        target=download_playlist_task, 
        args=(playlist_url, session_id)
    )
    download_thread.daemon = True
    download_thread.start()
    
    return jsonify({
        'success': True,
        'session_id': session_id,
        'message': 'Download started'
    })

@app.route('/download/<session_id>')
def download_file(session_id):
    if session_id not in active_downloads or active_downloads[session_id]['status'] != 'complete':
        return "Download not found or not complete", 404
        
    zip_path = active_downloads[session_id]['zip_path']
    if not os.path.exists(zip_path):
        return "Download file not found", 404
        
    return send_file(zip_path, as_attachment=True, download_name="spotify_playlist.zip")

@app.route('/check_status/<session_id>')
def check_status(session_id):
    if session_id not in active_downloads:
        return jsonify({'success': False, 'error': 'Session not found'})
        
    return jsonify({
        'success': True,
        'status': active_downloads[session_id]['status']
    })

if __name__ == '__main__':
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    socketio.run(app, debug=True)
