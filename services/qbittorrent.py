import qbittorrentapi
import logging
import os
import time
import urllib.parse

class QBittorrentService:
    def __init__(self, host, username, password, public_url_base, category=None):
        """
        Initialise le client qBittorrent avec la librairie officielle qbittorrent-api
        Docs: https://pypi.org/project/qbittorrent-api/
        """
        parsed = urllib.parse.urlparse(host if host.startswith('http') else f'http://{host}')

        self.host = host.rstrip('/')
        self.public_url_base = public_url_base.rstrip('/')
        self.category = category or os.getenv('QBITTORRENT_CATEGORY', 'frenchio')

        try:
            self.client = qbittorrentapi.Client(
                host=parsed.hostname or 'localhost',
                port=parsed.port or 8080,
                username=username,
                password=password,
                REQUESTS_ARGS={'timeout': 30}
            )
            logging.info(f"qBittorrent client created for {parsed.hostname}:{parsed.port}")
        except Exception as e:
            logging.error(f"Failed to create qBittorrent client: {e}")
            self.client = None

    def test_connection(self):
        """Test la connexion à qBittorrent"""
        if not self.client:
            return False

        try:
            version = self.client.app.version
            api_version = self.client.app.web_api_version
            logging.info(f"✅ qBittorrent connected: v{version} (API v{api_version})")
            return True
        except qbittorrentapi.LoginFailed as e:
            logging.error(f"❌ qBittorrent Login Failed: {e}")
            return False
        except qbittorrentapi.Forbidden403Error as e:
            logging.error(f"❌ qBittorrent 403 Forbidden: {e}")
            return False
        except Exception as e:
            logging.error(f"❌ qBittorrent Connection Error: {e}")
            return False

    def add_torrent(self, torrent_data, is_file=False):
        """
        Ajoute un torrent à qBittorrent avec les options de streaming séquentiel.
        """
        if not self.client:
            logging.error("qBittorrent client not initialized")
            return None

        try:
            # Pré-allocation disque requise pour le streaming en cours de téléchargement.
            # Sans elle, le fichier n'existe pas physiquement et le serveur HTTP retourne 404.
            # Désactivable via QBITTORRENT_PREALLOCATE=false
            if os.getenv('QBITTORRENT_PREALLOCATE', 'true').lower() != 'false':
                try:
                    self.client.app_set_preferences(prefs={'preallocate_all': True})
                    logging.info("🔧 Global preallocation enabled in qBittorrent")
                except Exception as e:
                    logging.warning(f"Could not enable global preallocation: {e}")

            # Créer la catégorie si elle n'existe pas
            try:
                self.client.torrents_create_category(name=self.category)
            except qbittorrentapi.Conflict409Error:
                pass
            except Exception as e:
                logging.warning(f"Could not create/verify category '{self.category}': {e}")

            streaming_opts = {
                'is_paused': False,
                'is_sequential_download': True,
                'is_first_last_piece_priority': True,
                'category': self.category,
            }

            if is_file:
                logging.info(f"Adding .torrent file ({len(torrent_data)} bytes) with streaming options")
                result = self.client.torrents_add(torrent_files=torrent_data, **streaming_opts)
            else:
                logging.info("Adding magnet/URL with streaming options")
                result = self.client.torrents_add(urls=torrent_data, **streaming_opts)

            if result == "Ok.":
                logging.info("✅ Torrent added successfully")
                return True
            else:
                logging.warning(f"Unexpected response from qBittorrent: {result}")
                return True

        except qbittorrentapi.Conflict409Error:
            logging.info("ℹ️ Torrent already exists in qBittorrent")
            return True
        except Exception as e:
            logging.error(f"❌ Failed to add torrent: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def configure_sequential(self, info_hash):
        """
        Force l'activation du téléchargement séquentiel et la priorité début/fin.
        """
        if not self.client:
            return False

        try:
            h = info_hash.lower()
            logging.info(f"🔧 Forcing streaming options for torrent {h[:8]}...")

            torrents = self.client.torrents_info(torrent_hashes=h)
            if torrents:
                torrent = torrents[0]
                seq_enabled = torrent.get('seq_dl', False)
                first_last_enabled = torrent.get('f_l_piece_prio', False)
            else:
                seq_enabled = False
                first_last_enabled = False

            logging.info(f"   Current state: sequential={seq_enabled}, first_last={first_last_enabled}")

            if not seq_enabled:
                try:
                    self.client.torrents_toggle_sequential_download(torrent_hashes=h)
                    logging.info(f"   ✅ Sequential download: OFF → ON")
                except Exception as e:
                    logging.error(f"   ❌ Failed to toggle sequential download: {e}")
                    raise
            else:
                logging.info(f"   ℹ️ Sequential download already ON")

            if not first_last_enabled:
                try:
                    self.client.torrents_toggle_first_last_piece_priority(torrent_hashes=h)
                    logging.info(f"   ✅ First/Last piece priority: OFF → ON")
                except Exception as e:
                    logging.error(f"   ❌ Failed to toggle first/last priority: {e}")
                    raise
            else:
                logging.info(f"   ℹ️ First/Last piece priority already ON")

            logging.info(f"✅ All streaming options configured for {h[:8]}...")
            return True

        except Exception as e:
            logging.error(f"❌ Failed to configure streaming options: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    def get_torrent_files(self, info_hash, max_retries=15, season=None, episode=None, fast_mode=False):
        """
        Récupère les fichiers d'un torrent et sélectionne le bon.
        """
        if not self.client:
            return None

        h = info_hash.lower()

        if fast_mode:
            max_retries = 8
            retry_delay = 0.5
        else:
            retry_delay = 1.0

        logging.info(f"🔍 Looking for files in torrent (fast_mode={fast_mode})...")

        for retry in range(max_retries):
            try:
                files = self.client.torrents_files(torrent_hash=h)

                if files:
                    logging.info(f"✅ Found {len(files)} files in torrent")

                    target_file = None

                    if season is not None and episode is not None:
                        import re
                        s_str = f"{int(season):02d}"
                        e_str = f"{int(episode):02d}"

                        patterns = [
                            re.compile(rf'S{s_str}E{e_str}', re.IGNORECASE),
                            re.compile(rf'{int(season)}x{e_str}', re.IGNORECASE),
                            re.compile(rf'E{e_str}', re.IGNORECASE)
                        ]

                        sorted_files = sorted(files, key=lambda x: x.size, reverse=True)

                        for f in sorted_files:
                            fname = f.name
                            for pat in patterns:
                                if pat.search(fname):
                                    target_file = fname
                                    logging.info(f"✅ Selected episode file: {fname}")
                                    break
                            if target_file:
                                break

                    if not target_file:
                        video_exts = ['.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v']
                        video_files = [f for f in files if any(f.name.lower().endswith(ext) for ext in video_exts)]

                        if video_files:
                            largest = max(video_files, key=lambda x: x.size)
                            target_file = largest.name
                            logging.info(f"✅ Selected largest video file: {target_file} ({largest.size} bytes)")
                        else:
                            largest = max(files, key=lambda x: x.size)
                            target_file = largest.name
                            logging.info(f"✅ Selected largest file: {target_file} ({largest.size} bytes)")

                    return target_file

            except Exception as e:
                if retry < max_retries - 1:
                    logging.debug(f"⏳ Waiting for metadata... ({retry + 1}/{max_retries})")
                    time.sleep(retry_delay)
                else:
                    logging.error(f"Failed to get torrent files: {e}")

            if retry < max_retries - 1:
                logging.debug(f"⏳ No files yet, retrying... ({retry + 1}/{max_retries})")
                time.sleep(retry_delay)

        logging.error(f"❌ Could not find files after {max_retries} retries")
        return None

    def verify_and_fix_streaming_options(self, info_hash):
        """
        Vérifie que les options de streaming sont bien activées, sinon les force à nouveau.
        """
        if not self.client:
            return False

        try:
            h = info_hash.lower()
            logging.info(f"🔍 Verifying streaming options for torrent {h[:8]}...")

            torrents = self.client.torrents_info(torrent_hashes=h)
            if torrents:
                torrent = torrents[0]
                seq_enabled = torrent.get('seq_dl', False)
                first_last_enabled = torrent.get('f_l_piece_prio', False)
            else:
                seq_enabled = False
                first_last_enabled = False

            logging.info(f"📊 Current status (from qBittorrent):")
            logging.info(f"   seq_dl = {seq_enabled} {'✅ ON' if seq_enabled else '❌ OFF'}")
            logging.info(f"   f_l_piece_prio = {first_last_enabled} {'✅ ON' if first_last_enabled else '❌ OFF'}")

            if not seq_enabled or not first_last_enabled:
                logging.warning("⚠️ Streaming options NOT applied correctly, forcing again...")
                self.configure_sequential(info_hash)

                time.sleep(0.5)
                torrents2 = self.client.torrents_info(torrent_hashes=h)
                if torrents2:
                    t2 = torrents2[0]
                    seq2 = t2.get('seq_dl', False)
                    first_last2 = t2.get('f_l_piece_prio', False)
                else:
                    seq2, first_last2 = False, False
                logging.info(f"📊 After second attempt: sequential={seq2}, first_last={first_last2}")
            else:
                logging.info("✅ Streaming options verified: ALL ON")

            return True

        except Exception as e:
            logging.error(f"❌ Failed to verify streaming options: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False

    def get_piece_size(self, info_hash):
        """Retourne la taille des pièces du torrent en bytes."""
        if not self.client:
            return None
        try:
            props = self.client.torrents_properties(torrent_hash=info_hash.lower())
            return props.piece_size
        except Exception as e:
            logging.warning(f"Could not get piece size for {info_hash[:8]}: {e}")
            return None

    def try_fast_path(self, info_hash, season=None, episode=None):
        """
        Vérifie si le torrent est déjà actif et retourne l'URL immédiatement.
        À appeler AVANT tout download de .torrent pour éviter la latence sur les seek libmpv.
        """
        h = info_hash.lower()
        try:
            if self.client:
                torrents = self.client.torrents_info(torrent_hashes=h)
                if torrents:
                    state = torrents[0].get('state', '')
                    bad_states = ('metaDL', 'allocating', 'missingFiles', 'error', 'checkingResumeData', 'checkingUP')
                    if state not in bad_states:
                        logging.info(f"⚡ Fast-path: torrent {h[:8]} already active (state={state})")
                        target_file = self.get_torrent_files(h, season=season, episode=episode, fast_mode=True)
                        if target_file:
                            safe_path = urllib.parse.quote(target_file)
                            return f"{self.public_url_base}/{safe_path}"
        except Exception as e:
            logging.warning(f"Fast-path check failed: {e}")
        return None

    def manage_stream(self, torrent_data, info_hash, is_file=False, season=None, episode=None):
        """
        Orchestre l'ajout du torrent et retourne l'URL de streaming IMMÉDIATEMENT.
        Le téléchargement se fait en arrière-plan, le player lit au fur et à mesure.
        """
        h = info_hash.lower()

        if not self.add_torrent(torrent_data, is_file):
            return None

        logging.info("⚡ Torrent added, preparing instant stream...")

        time.sleep(1.5)

        logging.info("🔧 Forcing streaming options...")
        self.configure_sequential(info_hash)

        target_file = self.get_torrent_files(info_hash, season=season, episode=episode, fast_mode=True)

        if not target_file:
            logging.error("❌ Could not identify target file")
            return None

        # Attendre que qBittorrent finisse d'allouer l'espace disque.
        # Sans cette attente, le fichier n'existe pas encore physiquement → HTTP 404.
        logging.info("⏳ Waiting for disk allocation to complete...")
        for i in range(15):
            try:
                torrents = self.client.torrents_info(torrent_hashes=h)
                if torrents:
                    state = torrents[0].get('state', '')
                    logging.info(f"   Torrent state: {state} ({i+1}/15)")
                    if state not in ('metaDL', 'allocating'):
                        logging.info("   ✅ Disk allocation completed")
                        break
            except Exception as e:
                logging.warning(f"Error checking torrent state: {e}")
            time.sleep(1.0)

        # Attendre que les premiers ET derniers pieces soient téléchargés.
        # libmpv seek automatiquement à la fin du MKV pour lire l'index/cues.
        # Sans ça, il lit des zéros (preallocated) → seek en boucle côté Stremio.
        logging.info("⏳ Waiting for first/last pieces (MKV index)...")
        for i in range(15):
            try:
                states = self.client.torrents_piece_states(torrent_hash=h)
                if states and len(states) >= 2:
                    first_ok = states[0] == 2
                    last_ok = states[-1] == 2
                    logging.info(f"   Pieces: first={'✅' if first_ok else '⏳'} last={'✅' if last_ok else '⏳'} ({i+1}/60)")
                    if first_ok and last_ok:
                        logging.info("   ✅ First/last pieces ready")
                        break
            except Exception as e:
                logging.warning(f"Piece state check failed: {e}")
                break
            time.sleep(1.0)

        safe_path = urllib.parse.quote(target_file)
        stream_url = f"{self.public_url_base}/{safe_path}"

        logging.info(f"🎬 STREAM ready: {stream_url}")
        return stream_url
