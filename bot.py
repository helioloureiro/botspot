#! /usr/bin/env python3

## std imports
import os
import argparse
import time
import threading
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import json
import signal

## external modules
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mastodon import Mastodon, errors
from odesli.Odesli import Odesli
import requests
import yaml
import lyricsgenius

## logging initializing
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

FIXED_INTERVAL = 60
# LONG_INTERVAL = 60 * 10


class MastodonSpotifyBot:
    def __init__(self, settings):

        logger.setLevel(settings["loglevel"].upper())

        self.settings = settings

        if self.settings["credentials"]["spotify"]["client ID"] is None:
            self.settings["credentials"]["spotify"]["client ID"] = os.environ.get("SPOTIFY_CLIENT_ID")

        if self.settings["credentials"]["spotify"]["client secret"] is None:
            self.settings["credentials"]["spotify"]["client secret"] = os.environ.get("SPOTIFY_CLIENT_SECRET")

        if self.settings["credentials"]["mastodon"]["access token"] is None:
            self.settings["credentials"]["mastodon"]["access token"] = os.environ.get("MASTODON_ACCESS_TOKEN")

        if self.settings["credentials"]["lyrics genius"]["token"] is None:
            self.settings["credentials"]["lyrics genius"]["token"] = os.environ.get("GENIUS_TOKEN")

    def run(self):
        logger.info("Authenticating on Spotify")
        self.authenticate_spotify()
        logger.info("Authenticating on Mastodon")
        self.authenticate_mastodon()
        last_song = None
        th = threading.Thread(target=callBackAction, args=(self.settings["callback"],))
        th.start()

        if not th.is_alive():
            logger.error("Callback service failed to start and serve")
            raise Exception("Failed to start callback service")

        with open("config.yaml") as conf:
            msg = yaml.safe_load(conf)

        while True:
            dados = self.get_recently_played()
            logger.debug("dados: " + str(dados))
            # envia para o Mastodon
            if dados is None:
                time.sleep(FIXED_INTERVAL)
                continue

            if 'item' in dados and dados['item'] is None:
                logger.warning("dados[\"item\"] is empty (null)")
                time.sleep(FIXED_INTERVAL)
                continue

            logger.debug("dados json:\n" + json.dumps(dados, indent=4) )

            if not "is_playing" in dados:
                logger.error("Missing entry \"is_playing\"")
                time.sleep(FIXED_INTERVAL)
                continue

            if not "progress_ms" in dados:
                logger.warning("Missing entry for \"progress_ms\"")
                time.sleep(FIXED_INTERVAL)
                continue

            if dados["is_playing"] == False:
                logger.error("Spotify isn't active right now...")
                if not self.settings["keepalive"]:
                    if th.is_alive():
                        signal.pthread_kill(th.ident, signal.SIGTERM)
                    sys.exit(0)
                time.sleep(FIXED_INTERVAL)
                continue

            progress_time = int(dados["progress_ms"]) / 1000.
            waiting_time_ms = int(dados["item"]["duration_ms"])
            waiting_time = waiting_time_ms / 1000.
            time_s = waiting_time - progress_time

            if dados["currently_playing_type"] != "track":
                logger.info("Not music playing: " + dados["currently_playing_type"])
                time.sleep(time_s)
                continue

            current_song = dados["item"]["name"]
            if last_song == current_song:
                logger.warning(f"Current song is the same as last song: {last_song}")
                time.sleep(time_s)
                continue

            artist = dados["item"]["artists"][0]["name"]
            song_url = dados["item"]["external_urls"]["spotify"]

            if not th.is_alive():
                logger.error("Callback server not running - exiting")
                sys.exit(1)

            # letra = self.lyrics(song=dados["item"]["name"], artist=dados["item"]["artists"][0]["name"]).lyrics

            # lyrica = letra.replace("EmbedShare Url:CopyEmbed:Copy", "")

            logger.info(f"sending update to mastodon: {current_song}")

            toot_msg = self.compose_message(
                current_song,
                artist,
                song_url
            )

            logger.debug("Posting: " + toot_msg)

            self.post_mastodon(toot_msg, current_song)

            logger.info(f"next song in {time_s} s")
            last_song = current_song
            time.sleep(time_s)


    def authenticate_spotify(self):
        "criação do objeto de autenticação do Spotify"
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=self.settings["credentials"]["spotify"]["client ID"],
                client_secret=self.settings["credentials"]["spotify"]["client secret"],
                redirect_uri=self.settings["callback"],
                scope=self.settings["scope"]
            ))

    def authenticate_mastodon(self):
        "autenticação no Mastodon"
        self.mstd = Mastodon(
            api_base_url = self.settings["credentials"]["mastodon"]["instance"],
            access_token = self.settings["credentials"]["mastodon"]["access token"]
        )

    def get_recently_played(self) -> dict:
        "função para pegar os dados do Spotify"
        generic_response = {
                "is_playing": False,
                "progress_ms": "60000"
            }

        try:
            results = self.sp.current_user_playing_track()
        except TypeError:
            return  generic_response
        except requests.exceptions.ReadTimeout:
            return generic_response
        except requests.exceptions.ConnectionError:
            return generic_response
        
        if results is None:
            return generic_response

        if results:
           if not "is_playing" in results:
               return  generic_response

        return results

    def encurta_url(self, url : str):
        "função para o gerenciador SongLink"
        try:
            return  Odesli().getByUrl(url).songLink
        except requests.exceptions.HTTPError as e:
            logger.error(f"Failed to generate generic song link: {e}")
            logger.info(f"Returning original song link: {url}")
            return url

    def compose_message(self, song_name, artist_info, song_link):
        "Render back the post message for Mastodon"

        tags = ""
        for tg in self.settings["hashtags"]:
            tags += f"#{tg}\n"

        return self.settings["post text"] % (song_name, artist_info, song_link, tags)

    def post_mastodon(self, message, song_name):
        "To handle mastodon post and errors - it will log as error but continue to process"
        spoiler = None
        if self.settings["Content Warning"]["enabled"]: 
            if isinstance(self.settings["Content Warning'"]["spoiler"], str):
                spoiler = self.settings["Content Warning'"]["spoiler"] % song_name
        try:
            self.mstd.status_post(message,
                                  visibility=self.settings["visibility"],
                                  spoiler_text=spoiler)
        except errors.MastodonServiceUnavailableError as e:
            logger.error(f"Failed to post on mastodon: {e}")


def callBackAction(localURL : str):
    "Função pra pegar o callback do spotify"
    # localURL format: http:// + localhost + : + <port> + <route>
    if not re.search("^http://localhost", localURL):
        logger.error("Failed to get callback URL")
        raise Exception(f"Callback em formato errado (esperado: http://localhost:9999/rota): {localURL}")

    port_and_route = re.sub("http://localhost:", "", localURL)
    (port, route) = port_and_route.split("/")
    port = int(port)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            logger.info("Callback service called")
            self.send_response(200)
            self.end_headers()
            client_ip, client_port = self.client_address
            reqpath = self.path.rstrip()
            logger.info(f"callback service: request from {client_ip}:{client_port} for {reqpath}")
            if reqpath == "/" + route:
                response = "some data"
            else:
                response = "Callback called"
            content = bytes(response.encode("utf-8"))
            self.wfile.write(content)

    # Bind to the local address only.
    logger.info(f"Starting callback webserver on port {port}")
    server_address = ('localhost', port)
    httpd = HTTPServer(server_address, Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    parse = argparse.ArgumentParser(description='Mastodon bot to post your spotify current listening song')
    parse.add_argument('--config', required=True, help="The yaml configuration file")
    args = parse.parse_args()

    with open(args.config) as config:
        settings = yaml.safe_load(config)

    bot = MastodonSpotifyBot(settings)
    bot.run()
