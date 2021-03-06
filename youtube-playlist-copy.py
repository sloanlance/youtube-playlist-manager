#!/usr/bin/python2
# -*- coding: utf-8 -*-

import httplib
import httplib2
import os
import random
import sys
import argparse
import logging

from apiclient.discovery import build
from apiclient.errors import HttpError
from apiclient.http import BatchHttpRequest

from oauth2client.file import Storage
from oauth2client.client import flow_from_clientsecrets
from oauth2client.tools import run

import simplejson as json


# Maximum number of results YouTube allows us to retrieve in one list request
MAX_RESULTS = 50

# Explicitly tell the underlying HTTP transport library not to retry, since
# we are handling retry logic ourselves.
httplib2.RETRIES = 1

# Maximum number of times to retry before giving up.
MAX_RETRIES = 10

# Always retry when these exceptions are raised.
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, IOError, httplib.NotConnected,
	httplib.IncompleteRead, httplib.ImproperConnectionState,
	httplib.CannotSendRequest, httplib.CannotSendHeader,
	httplib.ResponseNotReady, httplib.BadStatusLine)

# Always retry when an apiclient.errors.HttpError with one of these status
# codes is raised.
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

# CLIENT_SECRETS_FILE, name of a file containing the OAuth 2.0 information for
# this application, including client_id and client_secret. You can acquire an
# ID/secret pair from the API Access tab on the Google APIs Console
#   http://code.google.com/apis/console#access
# For more information about using OAuth2 to access Google APIs, please visit:
#   https://developers.google.com/accounts/docs/OAuth2
# For more information about the client_secrets.json file format, please visit:
#   https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
# Please ensure that you have enabled the YouTube Data API for your project.
CLIENT_SECRETS_FILE = "client_secrets.json"

# An OAuth 2 access scope that allows for full read/write access.
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

# Helpful message to display if the CLIENT_SECRETS_FILE is missing.
MISSING_CLIENT_SECRETS_MESSAGE = """
WARNING: Please configure OAuth 2.0

To make this sample run you will need to populate the client_secrets.json file
found at:

   %s

with information from the APIs Console
https://code.google.com/apis/console#access

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
""" % os.path.abspath(os.path.join(os.path.dirname(__file__),
                      CLIENT_SECRETS_FILE))

def get_authenticated_service():
	http = httplib2.Http(cache=".cache")
	flow = flow_from_clientsecrets(CLIENT_SECRETS_FILE, scope=YOUTUBE_SCOPE,
	                               message=MISSING_CLIENT_SECRETS_MESSAGE)

	storage = Storage("%s-oauth2.json" % sys.argv[0])
	credentials = storage.get()

	if credentials is None or credentials.invalid:
		credentials = run(flow, storage)

	return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION,
	             http=credentials.authorize(http))


def copy_playlist(youtube, args):
	id = args.id

	playlists_req = youtube.playlists().list(
		part = "id,snippet,contentDetails",
		id = id,
		maxResults = 1,
	)

	playlists = playlists_req.execute()
	if "nextPageToken" in playlists:
		sys.stderr.write("More than one playlist with id {}\n".format(id))
		sys.exit(1)

	playlist = playlists["items"][0]

	sys.stderr.write("== ")
	sys.stderr.write(playlist["snippet"]["title"])
	sys.stderr.write(" ==\n")

	videos_req = youtube.playlistItems().list(
		part = "id,contentDetails,snippet",
		playlistId = id,
		maxResults = MAX_RESULTS,
	)

	sys.stderr.write("Reading: ")

	my_videos = []
	while videos_req:
		videos = videos_req.execute()

		for video in videos["items"]:
			sys.stderr.write(".")
			my_videos.append(video)

		videos_req = youtube.playlistItems().list_next(videos_req, videos)

	sys.stderr.write("\n")

	my_videos.sort(key = lambda video: video["snippet"]["position"])

	position = 0
	for video in my_videos:
		if args.debug and video["snippet"]["position"] != position:
			sys.stderr.write("Fixing position: {old} -> {new}\n".format(old = video["snippet"]["position"], new = position))
		video["snippet"]["position"] = position
		position = position + 1

	if args.prefix:
		playlist["snippet"]["title"] = args.prefix + playlist["snippet"]["title"]
	# The channelId found in the original playlist is not ours:
	del playlist["snippet"]["channelId"]

	# Try not to confuse YouTube:
	del playlist["id"]
	del playlist["etag"]

	del playlist["contentDetails"]

	playlist_new = None
	if args.pretend:
		playlist_new = playlist
	else:
		playlist_req = youtube.playlists().insert(
			part = "snippet,status",
			body = playlist,
		)
		playlist_new = playlist_req.execute()

	request_payloads = {}
	insert_requests = []
	finished_requests = []

	def skip(request_id):
		finished_requests.append(request_id)
		found_current = False
		for insert_request_id in insert_requests:
			if insert_request_id == request_id:
				found_current = True
				continue
			if not found_current:
				continue
			video = request_payloads[insert_request_id]
			video["snippet"]["position"] = video["snippet"]["position"] - 1

	def insert_video(request_id, response, exception):
		payload = request_payloads[request_id]
		position = payload["snippet"]["position"]
		video_id = payload["snippet"]["resourceId"]["videoId"]

		if exception:
			if not args.debug:
				sys.stderr.write("\n")

			if exception.resp.status == httplib.FORBIDDEN:
				sys.stderr.write("WARNING: Video {id} private, skipping\n".format(id = video_id))
				if args.debug:
					sys.stderr.write("Filling gap at position {position}\n".format(position = position))
				skip(request_id)
			elif exception.resp.status == httplib.NOT_FOUND:
				sys.stderr.write("WARNING: Video {id} deleted, skipping\n".format(id = video_id))
				if args.debug:
					sys.stderr.write("Filling gap at position {position}\n".format(position = position))
				skip(request_id)
			elif exception.resp.status in RETRIABLE_STATUS_CODES:
				sys.stderr.write("WARNING: Server returned status {status} for video {id}, trying again\n".format(status = exception.resp.status, id = video_id))
			else:
				if args.debug:
					sys.stderr.write("Error inserting video {id}:\n{video}\n".format(id = video_id, video = video))
				raise exception
		else:
			if args.debug:
				sys.stderr.write("Inserted video {id}\n".format(id = video_id))
			else:
				sys.stderr.write(".")
			finished_requests.append(request_id)

	for video in my_videos:
		request_id = video["id"]
		request_payloads[request_id] = video

		# Link to the new playlist instead of the old:
		video["snippet"]["playlistId"] = playlist_new["id"]
		# The channelId found in the original video is not ours:
		del video["snippet"]["channelId"]

		# Try not to confuse YouTube:
		del video["id"]
		del video["etag"]

		if not args.pretend:
			insert_requests.append(request_id)

	sys.stderr.write("Writing: ")

	while insert_requests:
		if args.batch:
			batch_req = BatchHttpRequest(callback = insert_video)

		for request_id in insert_requests:
			video = request_payloads[request_id]

			video_req = youtube.playlistItems().insert(
				part = "contentDetails,snippet",
				body = video,
			)

			if args.batch:
				batch_req.add(video_req, request_id = request_id)
			else:
				response = None
				try:
					response = video_req.execute()
				except Exception as e:
					insert_video(request_id, None, e)
				else:
					insert_video(request_id, response, None)

		if args.batch:
			batch_req.execute()

		for request_id in finished_requests:
			insert_requests.remove(request_id)
		del finished_requests[:]

		sys.stderr.write("\n")

	sys.stderr.write("\n")

if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument("id")
	parser.add_argument("-b", "--batch", action="store_true")
	parser.add_argument("-d", "--debug", action="store_true")
	parser.add_argument("-p", "--pretend", action="store_true")
	parser.add_argument("--prefix")
	args = parser.parse_args()

	if args.debug:
		sys.stderr.write("Debugging ...\n")
		logger = logging.getLogger()
		logger.setLevel(logging.INFO)
		#httplib2.debuglevel = 4
	if args.batch:
		sys.stderr.write("Batching ...\n")
	if args.pretend:
		sys.stderr.write("Pretending ...\n")

	youtube = get_authenticated_service()
	copy_playlist(youtube, args)
