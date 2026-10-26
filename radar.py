#!/usr/bin/env python3
"""Radar : découvre ce qui remplit ton disque, directement dans ton navigateur.

Usage : python3 radar.py [dossier] [--port N] [--no-open]

Aucune dépendance : uniquement la bibliothèque standard de Python 3.8+.
"""

import argparse
import heapq
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
BIG_FILE = 512 * 1024   # en dessous, les fichiers d'un dossier sont regroupés
TOP_FILES = 200
LEVEL_LIMIT = 40        # éléments détaillés par niveau, le reste est regroupé
TOKEN = secrets.token_urlsafe(24)

CATEGORIES = {
    'Vidéos': 'mp4 mov m4v mkv avi webm wmv flv mts',
    'Images': 'jpg jpeg png heic heif gif webp tif tiff raw cr2 cr3 nef arw dng psd svg bmp',
    'Audio': 'mp3 wav flac aac m4a aiff aif ogg opus',
    'Documents': 'pdf doc docx pages key ppt pptx xls xlsx numbers txt md rtf epub csv',
    'Archives et installeurs': 'zip dmg iso pkg rar 7z tar gz tgz bz2 xz xip ipa',
    'Code': 'js ts jsx tsx py swift java c cpp h hpp json html css rs go rb php kt',
}
EXT_CAT = {e: c for c, exts in CATEGORIES.items() for e in exts.split()}
INSTALLERS = {'dmg', 'pkg', 'xip', 'iso'}

# Dossiers connus pour grossir sans qu'on s'en rende compte.
# (clé, titre, explication, test sur (chemin absolu, nom))
CLEANUP = [
    ('trash', 'Corbeille',
     "Fichiers déjà supprimés : ils occupent encore le disque tant que la corbeille n'est pas vidée.",
     lambda p, n: p.endswith('/.Trash')),
    ('caches', "Caches d'applications",
     "Recréés automatiquement par les apps quand elles en ont besoin.",
     lambda p, n: p.endswith('/Library/Caches')),
    ('node_modules', 'node_modules',
     "Dépendances JavaScript de projets : « npm install » les réinstalle.",
     lambda p, n: n == 'node_modules'),
    ('xcode', 'Xcode DerivedData',
     "Fichiers de compilation Xcode, regénérés à la prochaine compilation.",
     lambda p, n: p.endswith('/Xcode/DerivedData')),
    ('simulators', 'Simulateurs iOS',
     "Simulateurs Xcode : « xcrun simctl delete unavailable » supprime ceux qui ne servent plus.",
     lambda p, n: p.endswith('/Developer/CoreSimulator/Devices')),
    ('backups', 'Sauvegardes iPhone / iPad',
     "Anciennes sauvegardes d'appareils, à gérer depuis le Finder (onglet de l'appareil).",
     lambda p, n: p.endswith('/MobileSync/Backup')),
    ('python', 'Environnements Python',
     "Environnements virtuels et caches Python, recréables à tout moment.",
     lambda p, n: n in ('.venv', 'venv', '__pycache__', '.mypy_cache', '.pytest_cache')),
    ('packages', 'Caches de gestionnaires de paquets',
     "Caches npm, pip, Gradle, Cargo, CocoaPods… retéléchargés au besoin.",
     lambda p, n: n in ('.npm', '.gradle', '.cocoapods', '.cache')),
    ('docker', 'Docker',
     "Images et volumes Docker : « docker system prune » libère de la place.",
     lambda p, n: p.endswith('/Containers/com.docker.docker')),
]


class Node:
    __slots__ = ('name', 'parent', 'size', 'count', 'dirs', 'files', 'small', 'nsmall')

    def __init__(self, name, parent):
        self.name, self.parent = name, parent
        self.size = self.count = self.small = self.nsmall = 0
        self.dirs = []
        self.files = []  # [nom, taille] des fichiers d'au moins BIG_FILE


def join(rel, name):
    return rel + '/' + name if rel else name


class Scan:
    def __init__(self, root):
        self.root = root
        self.state = 'scanning'
        self.files = self.bytes = self.errors = 0
        self.current = root
        self.started, self.finished = time.time(), None
        self.tree = None
        self.top = []          # tas (taille, chemin absolu)
        self.cats = {}
        self.cleanup = {}
        self.installers = []
        self._seen = set()

    # -- Analyse ------------------------------------------------------------

    def run(self):
        dev = os.lstat(self.root).st_dev
        self.tree = self._scan(self.root, os.path.basename(self.root) or self.root, None, dev, None)
        self._find_cleanup(self.tree, self.root)
        if self.installers:
            self._add_group('installers', 'Installeurs oubliés',
                            "Fichiers .dmg / .pkg : inutiles une fois l'application installée.",
                            self.installers)
        self.finished = time.time()
        self.state = 'done'

    def _scan(self, path, name, parent, dev, cat):
        node = Node(name, parent)
        self.current = path
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            self.errors += 1
            return node
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                self.errors += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != dev:  # ne pas traverser les autres volumes
                    continue
                sub = cat or ('Applications' if e.name.endswith('.app') else None)
                child = self._scan(e.path, e.name, node, dev, sub)
                node.dirs.append(child)
                node.size += child.size
                node.count += child.count
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_nlink > 1:  # un fichier avec plusieurs liens n'est compté qu'une fois
                key = (st.st_dev, st.st_ino)
                if key in self._seen:
                    continue
                self._seen.add(key)
            size = st.st_blocks * 512  # espace réellement occupé
            node.size += size
            node.count += 1
            self.files += 1
            self.bytes += size
            ext = os.path.splitext(e.name)[1][1:].lower()
            c = cat or EXT_CAT.get(ext, 'Autres')
            self.cats[c] = self.cats.get(c, 0) + size
            if size >= BIG_FILE:
                node.files.append([e.name, size])
                item = (size, e.path)
                if len(self.top) < TOP_FILES:
                    heapq.heappush(self.top, item)
                else:
                    heapq.heappushpop(self.top, item)
                if ext in INSTALLERS:
                    self.installers.append((size, e.path))
            else:
                node.small += size
                node.nsmall += 1
        return node

    def _find_cleanup(self, node, path):
        for child in node.dirs:
            p = os.path.join(path, child.name)
            rule = next((r for r in CLEANUP if r[3](p, child.name)), None)
            if rule:
                if child.size:
                    self.cleanup.setdefault(rule[0], (rule, []))[1].append((child.size, p))
            else:
                self._find_cleanup(child, p)

    def _add_group(self, key, title, why, items):
        self.cleanup[key] = ((key, title, why, None), items)

    # -- Lecture ------------------------------------------------------------

    def rel(self, path):
        return os.path.relpath(path, self.root)

    def find(self, rel):
        """Renvoie (dossier, None) pour un dossier, (parent, [nom, taille]) pour un fichier."""
        node = self.tree
        if not rel:
            return node, None
        parts = rel.split('/')
        for i, part in enumerate(parts):
            nxt = next((d for d in node.dirs if d.name == part), None)
            if nxt is None:
                if i == len(parts) - 1:
                    f = next((f for f in node.files if f[0] == part), None)
                    if f:
                        return node, f
                return None, None
            node = nxt
        return node, None

    def view(self, node, rel, depth):
        out = {'name': node.name, 'path': rel, 'size': node.size, 'count': node.count, 'type': 'dir'}
        if depth == 0:
            return out
        kids = [(d.size, d) for d in node.dirs] + [(f[1], f) for f in node.files]
        kids.sort(key=lambda k: k[0], reverse=True)
        children = []
        for size, k in kids[:LEVEL_LIMIT]:
            if isinstance(k, Node):
                children.append(self.view(k, join(rel, k.name), depth - 1))
            else:
                children.append({'name': k[0], 'path': join(rel, k[0]), 'size': size, 'type': 'file'})
        rest = kids[LEVEL_LIMIT:]
        if rest:
            children.append({'name': '%d autres éléments' % len(rest),
                             'size': sum(r[0] for r in rest), 'type': 'rest'})
        if node.small:
            children.append({'name': 'Petits fichiers (%d)' % node.nsmall,
                             'size': node.small, 'type': 'rest'})
        out['children'] = [c for c in children if c['size'] > 0]
        return out

    def summary(self):
        disk = shutil.disk_usage(self.root)
        groups = []
        for (key, title, why, _), items in self.cleanup.values():
            items = sorted(items, reverse=True)
            size = sum(s for s, _ in items)
            if size < 1_000_000:
                continue
            groups.append({'key': key, 'title': title, 'why': why, 'size': size, 'n': len(items),
                           'items': [{'path': self.rel(p), 'size': s} for s, p in items[:30]]})
        groups.sort(key=lambda g: g['size'], reverse=True)
        return {
            'root': self.root,
            'name': self.tree.name,
            'total': self.tree.size,
            'count': self.tree.count,
            'elapsed': round(self.finished - self.started, 1),
            'errors': self.errors,
            'disk': {'total': disk.total, 'used': disk.used, 'free': disk.free},
            'categories': sorted(({'name': k, 'size': v} for k, v in self.cats.items() if v),
                                 key=lambda c: c['size'], reverse=True),
            'top': [{'path': self.rel(p), 'size': s} for s, p in sorted(self.top, reverse=True)[:60]],
            'cleanup': groups,
        }

    # -- Modification ---------------------------------------------------------

    def remove(self, rel):
        """Retire un élément de l'arbre après sa mise à la corbeille."""
        node, f = self.find(rel)
        if f is not None:
            node.files.remove(f)
            size, count, up = f[1], 1, node
        else:
            size, count, up = node.size, node.count, node.parent
            up.dirs.remove(node)
        while up is not None:
            up.size -= size
            up.count -= count
            up = up.parent
        gone = os.path.join(self.root, rel)
        inside = lambda p: p == gone or p.startswith(gone + '/')
        self.top = [t for t in self.top if not inside(t[1])]
        heapq.heapify(self.top)
        for key, (rule, items) in list(self.cleanup.items()):
            items[:] = [(s, p) for s, p in items if not inside(p)]
        return size


SCAN = None
LOCK = threading.Lock()


def start_scan(root):
    global SCAN
    with LOCK:
        SCAN = Scan(root)
        threading.Thread(target=SCAN.run, daemon=True).start()


def move_to_trash(path):
    if sys.platform != 'darwin':
        raise RuntimeError('La mise à la corbeille est disponible uniquement sur macOS.')
    script = ('on run argv\n'
              'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\n'
              'end run')
    r = subprocess.run(['osascript', '-e', script, path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "Le Finder a refusé l'opération.")


def reveal(path):
    if sys.platform == 'darwin':
        subprocess.run(['open', '-R', path])
    else:
        subprocess.run(['xdg-open', os.path.dirname(path)])


class Handler(BaseHTTPRequestHandler):
    server_version = 'Radar'

    def log_message(self, *args):
        pass

    def send(self, body, code=200, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        # Protège contre le « DNS rebinding » : seul l'hôte local est accepté.
        port = self.server.server_port
        return self.headers.get('Host') in ('127.0.0.1:%d' % port, 'localhost:%d' % port)

    def do_GET(self):
        if not self.host_ok():
            return self.send({'error': 'Hôte refusé'}, 403)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        scan = SCAN
        if url.path == '/':
            with open(os.path.join(HERE, 'radar.html'), encoding='utf-8') as fh:
                html = fh.read().replace('__TOKEN__', TOKEN)
            return self.send(html.encode(), ctype='text/html; charset=utf-8')
        if url.path == '/api/status':
            return self.send({
                'state': scan.state, 'root': scan.root, 'files': scan.files, 'bytes': scan.bytes,
                'current': scan.current, 'errors': scan.errors,
                'elapsed': round((scan.finished or time.time()) - scan.started, 1),
            })
        if scan.state != 'done':
            return self.send({'error': 'Analyse en cours'}, 409)
        if url.path == '/api/summary':
            return self.send(scan.summary())
        if url.path == '/api/dir':
            rel = q.get('path', [''])[0].strip('/')
            depth = max(1, min(6, int(q.get('depth', ['4'])[0])))
            node, f = scan.find(rel)
            if node is None or f is not None:
                return self.send({'error': 'Dossier introuvable'}, 404)
            return self.send(scan.view(node, rel, depth))
        self.send({'error': 'Introuvable'}, 404)

    def do_POST(self):
        if not self.host_ok() or self.headers.get('X-Radar-Token') != TOKEN:
            return self.send({'error': 'Accès refusé'}, 403)
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except ValueError:
            return self.send({'error': 'Requête invalide'}, 400)
        path = urlparse(self.path).path
        if path == '/api/rescan':
            start_scan(SCAN.root)
            return self.send({'ok': True})
        scan = SCAN
        if scan.state != 'done':
            return self.send({'error': 'Analyse en cours'}, 409)
        rel = str(data.get('path', '')).strip('/')
        node, f = scan.find(rel) if rel and '..' not in rel.split('/') else (None, None)
        if node is None:
            return self.send({'error': 'Élément introuvable'}, 404)
        target = os.path.join(scan.root, rel)
        try:
            if path == '/api/reveal':
                reveal(target)
                return self.send({'ok': True})
            if path == '/api/trash':
                move_to_trash(target)
                with LOCK:
                    freed = scan.remove(rel)
                return self.send({'ok': True, 'freed': freed})
        except RuntimeError as err:
            return self.send({'error': str(err)}, 500)
        self.send({'error': 'Introuvable'}, 404)


def main():
    ap = argparse.ArgumentParser(description='Découvre ce qui remplit ton disque.')
    ap.add_argument('dossier', nargs='?', default='~', help='dossier à analyser (par défaut : ton dossier personnel)')
    ap.add_argument('--port', type=int, default=0, help='port du serveur local (par défaut : au hasard)')
    ap.add_argument('--no-open', action='store_true', help="ne pas ouvrir le navigateur")
    args = ap.parse_args()

    root = os.path.realpath(os.path.expanduser(args.dossier))
    if not os.path.isdir(root):
        sys.exit('Dossier introuvable : %s' % root)
    sys.setrecursionlimit(20000)

    start_scan(root)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = 'http://127.0.0.1:%d/' % server.server_port
    print('Radar analyse %s\nOuvre %s  (Ctrl+C pour quitter)' % (root, url))
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nÀ bientôt !')


if __name__ == '__main__':
    main()
