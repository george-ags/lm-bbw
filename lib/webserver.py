import http.server
import _thread as thread
import os
import urllib.parse
import html
import io
import sys

class GalleryHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    Custom handler that displays a grid of images instead of a file list.
    """
    def list_directory(self, path):
        """
        Overrides the default list_directory to generate an HTML image gallery.
        """
        try:
            list_dir = os.listdir(path)
        except OSError:
            self.send_error(http.HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
            
        # --- FIX: Sort by name descending (Newest First) ---
        list_dir.sort(key=lambda a: a.lower(), reverse=True)
        # ---------------------------------------------------
        
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
        r.append('body { font-family: sans-serif; background: #222; color: #eee; }')
        r.append('h1 { text-align: center; }')
        r.append('.gallery { display: flex; flex-wrap: wrap; justify-content: center; gap: 15px; }')
        r.append('.item { background: #333; padding: 10px; border-radius: 8px; text-align: center; }')
        r.append('img { max-height: 200px; max-width: 300px; display: block; border-radius: 4px; }')
        r.append('a { color: #88c0d0; text-decoration: none; display: block; margin-top: 5px; font-size: 0.9em; }')
        r.append('a:hover { text-decoration: underline; color: #fff; }')
        r.append('</style>')
        r.append('</head><body>')
        r.append(f'<h1>{title}</h1>')
        r.append('<hr>')
        
        # Link to parent directory
        r.append('<div style="margin-bottom: 20px; text-align:center;"><a href="../" style="font-size:1.2em;">&larr; Back / Parent Directory</a></div>')
        
        r.append('<div class="gallery">')

        for name in list_dir:
            fullname = os.path.join(path, name)
            displayname = linkname = name
            
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
            if os.path.islink(fullname):
                displayname = name + "@"

            # URL Encode the link
            url_link = urllib.parse.quote(linkname)
            
            # Check if image
            lower_name = name.lower()
            is_image = lower_name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))
            
            if is_image:
                # Render Image Card
                r.append('<div class="item">')
                r.append(f'<a href="{url_link}"><img src="{url_link}" alt="{html.escape(displayname)}" loading="lazy"></a>')
                r.append(f'<a href="{url_link}">{html.escape(displayname)}</a>')
                r.append('</div>')
            elif os.path.isdir(fullname):
                # Render Directory Link
                r.append('<div class="item" style="display:flex; align-items:center; justify-content:center; width:200px; height:100px;">')
                r.append(f'<a href="{url_link}" style="font-size:1.5em; font-weight:bold;">📂 {html.escape(displayname)}</a>')
                r.append('</div>')
            # Skip non-image files to keep gallery clean (or add else block to show them as text links)

        r.append('</div>\n<hr>\n</body>\n</html>\n')
        
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
    # Dynamically bind the directory to our custom Gallery Handler
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