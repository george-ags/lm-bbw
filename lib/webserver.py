import http.server
import _thread as thread
import os
import urllib.parse
import html
import io
import sys

class GalleryHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    Custom handler that displays a grid of images sorted by File Modification Time (Newest First).
    """
    def list_directory(self, path):
        try:
            list_dir = os.listdir(path)
        except OSError:
            self.send_error(http.HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
            
        # --- FIX: Sort by File Modification Time (Newest First) ---
        def get_mtime_key(filename):
            try:
                # Construct full path to get metadata
                fullname = os.path.join(path, filename)
                return os.path.getmtime(fullname)
            except OSError:
                return 0 # Push unreadable files to bottom

        list_dir.sort(key=get_mtime_key, reverse=True)
        # ----------------------------------------------------------
        
        try:
            displaypath = urllib.parse.unquote(self.path, errors='surrogatepass')
        except UnicodeDecodeError:
            displaypath = urllib.parse.unquote(self.path)
            
        displaypath = html.escape(displaypath, quote=False)
        enc = sys.getfilesystemencoding()
        title = 'Shot History: %s' % displaypath
        
        # Build HTML
        r = []
        r.append('<!DOCTYPE html>')
        r.append('<html><head>')
        r.append(f'<title>{title}</title>')
        r.append('<meta http-equiv="Content-Type" content="text/html; charset=utf-8">')
        r.append('<style>')
        r.append('body { font-family: sans-serif; background: #222; color: #eee; margin: 0; padding: 20px; }')
        r.append('h1 { text-align: center; margin-bottom: 30px; }')
        r.append('.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; padding: 0 20px; }')
        r.append('.item { background: #333; padding: 15px; border-radius: 8px; text-align: center; transition: transform 0.2s; }')
        r.append('.item:hover { transform: scale(1.02); background: #3a3a3a; }')
        r.append('img { width: 100%; height: auto; display: block; border-radius: 4px; border: 1px solid #444; }')
        r.append('a { color: #88c0d0; text-decoration: none; display: block; margin-top: 10px; font-size: 0.9em; word-wrap: break-word; }')
        r.append('a:hover { text-decoration: underline; color: #fff; }')
        r.append('.nav { margin-bottom: 20px; text-align:center; }')
        r.append('.nav a { font-size: 1.2em; display: inline-block; padding: 10px 20px; background: #444; border-radius: 5px; }')
        r.append('</style>')
        r.append('</head><body>')
        
        r.append(f'<h1>{title}</h1>')
        
        # Link to parent directory
        r.append('<div class="nav"><a href="../">&larr; Back / Parent Directory</a></div>')
        
        r.append('<div class="gallery">')

        for name in list_dir:
            fullname = os.path.join(path, name)
            displayname = linkname = name
            
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
            if os.path.islink(fullname):
                displayname = name + "@"

            url_link = urllib.parse.quote(linkname)
            
            lower_name = name.lower()
            is_image = lower_name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))
            
            if is_image:
                r.append('<div class="item">')
                # Wrap image in link to full size
                r.append(f'<a href="{url_link}"><img src="{url_link}" alt="{html.escape(displayname)}" loading="lazy"></a>')
                # Clean up filename for display (replace underscores with spaces for readability)
                pretty_name = displayname.replace('_', ' ').replace('.png', '')
                r.append(f'<a href="{url_link}">{html.escape(pretty_name)}</a>')
                r.append('</div>')
            elif os.path.isdir(fullname):
                r.append('<div class="item">')
                r.append(f'<a href="{url_link}" style="font-size:3em; margin: 20px 0;">📂</a>')
                r.append(f'<a href="{url_link}">{html.escape(displayname)}</a>')
                r.append('</div>')

        r.append('</div>\n</body>\n</html>\n')
        
        encoded = ''.join(r).encode(enc, 'surrogateescape')
        f = io.BytesIO()
        f.write(encoded)
        f.seek(0)
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "text/html; charset=%s" % enc)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return f

def _create_handler(directory):
    def _init(self, *args, **kwargs):
        return GalleryHTTPRequestHandler.__init__(self, *args, directory=self.directory, **kwargs)
        
    return type(f'GalleryHandlerFrom<{directory}>',
                (GalleryHTTPRequestHandler,),
                {'__init__': _init, 'directory': directory})

class WebServer:
    def __init__(self, directory: str, port: int):
        self.port = port
        self.directory = directory

    def start(self):
        thread.start_new_thread(self._create_server, ())

    def _create_server(self):
        handler = _create_handler(directory=self.directory)
        server = http.server.ThreadingHTTPServer(("", self.port), handler)
        server.serve_forever()