#! /usr/bin/env python

import json
import re
import time

import requests

from config import WithConfig
from utils.with_logger import WithLogger


class Slacker(WithLogger, WithConfig):

    def __init__(self, token=None, init=True):
        """
        slack name is the short name of the slack (preceding '.slack.com')
        token should be a Slack API Token.
        """
        self.token = token or self.config.api_token  # not sb_token!
        assert self.token, "Token should not be blank"
        self.url = self.api_url()
        self.session = requests.Session()
        if init:
            self.get_users()
            self.get_channels()

    def get_emojis(self):
        url = self.url + "emoji.list?token={}".format(self.token)
        payload = self.session.get(url).json()
        return payload

    def get_user(self, uid):
        url = self.url + "users.info?token={}&user={}".format(self.token, uid)
        payload = self.session.get(url).json()
        return payload

    def get_users(self):
        users = self.get_all_user_objects()
        self.users_by_id = {x['id']: x['name'] for x in users}
        self.users_by_name = {x['name']: x['id'] for x in users}
        self.restricted_users = [x['id'] for x in users if x.get('is_restricted')]
        self.ultra_restricted_users = [x['id'] for x in users if x.get('is_ultra_restricted')]
        self.all_restricted_users = set(self.restricted_users + self.ultra_restricted_users)
        self.logger.debug("All restricted user names: %s", ', '.join([self.users_by_id[x] for x in self.all_restricted_users]))
        return users

    def asciify(self, text):
        return ''.join([x for x in list(text) if ord(x) in range(128)])

    def add_channel_markup(self, channel_name, fail_silently=True):
        channel_id = self.get_channelid(channel_name)
        if channel_id:
            return "<#{}|{}>".format(channel_id, channel_name)
        else:
            if fail_silently:
                return "#{}".format(channel_name)

    def get_messages_in_time_range(self, oldest, cid, latest=None):
        assert cid in self.channels_by_id, "Unknown channel ID {}".format(cid)
        cname = self.channels_by_id[cid]
        messages = []
        done = False
        while not done:
            murl = self.url + "channels.history?oldest={}&token={}&channel={}".format(oldest, self.token, cid)
            if latest:
                murl += "&latest={}".format(latest)
            else:
                murl += "&latest={}".format(int(time.time()))
            response = self.session.get(murl)
            payload = response.json()
            if payload.get('error') == 'ratelimited':
                retry_after = int(response.headers['Retry-After'])
                self.logger.warn('Ratelimited. Sleeping %s', retry_after)
                time.sleep(retry_after)
                continue
            messages += payload['messages']
            if payload['has_more'] is False:
                done = True
                continue
            ts = [float(x['ts']) for x in messages]
            earliest = min(ts)
            latest = earliest
        messages.sort(key=lambda x: float(x['ts']))
        for message in messages:
            message['channel'] = cname
        return messages

    def replace_id(self, cid):
        """
        Assuming either a #channelid or @personid, replace them with #channelname or @username
        """
        stripped = cid[1:]
        first = cid[0]
        if first == "#":
            m = [x for x in self.channels if self.channels[x] == stripped]
            if m:
                return "#" + m[0]
        elif first == "@":
            # occasionally input will have the format "userid|name".
            #  in case the name changed at some point,
            #  lookup user by userid in users_by_id
            if "|" in stripped:
                uname_parts = stripped.split("|")
                uname = self.users_by_id[uname_parts[0]]
            else:
                uname = self.users_by_id[stripped]
            if uname:
                return "@" + uname
        return cid

    def detokenize(self, message):
        new = []
        tokens = re.split("(<.*?>)", message)
        for token in tokens:
            if len(token) > 3 and token[0] == "<" and token[-1] == ">":
                token = self.replace_id(token[1:-1])
            new.append(token)
        message = " ".join(new)
        return message

    def api_url(self):
        return "https://{}.slack.com/api/".format(self.config.slack_name)

    def get_channels(self, exclude_archived=True):
        """
        return a {channel_name: channel_id} dictionary
        if exclude_archived (default: True), only shows non-archived channels
        """
        channels = self.get_all_channel_objects(exclude_archived=exclude_archived)
        self.channels_by_id = {x['id']: x['name'] for x in channels}
        self.channels_by_name = {x['name']: x['id'] for x in channels}
        self.channels = self.channels_by_name

    def get_channelid(self, channel_name):
        return self.channels_by_name.get(channel_name)

    def channel_exists(self, channel_name):
        try:
            # strip leading "#" if it exists, as Slack returns all channels without them
            if channel_name[0] == "#":
                channel = channel_name[1:]
            else:
                channel = channel_name
            return self.channels_by_name[channel]
        except KeyError:  # channel not found
            return None

    def delete_message(self, cid, message_timestamp):
        url_template = self.url + "chat.delete?token={}&channel={}&ts={}"
        url = url_template.format(self.token, cid, message_timestamp)
        ret = self.session.get(url).json()
        if not ret['ok']:
            self.logger.error("Failed to delete message; error: %s", ret)
        return ret['ok']

    def get_channel_members_ids(self, channel_name):
        """
        returns an array of member IDs for channel_name
        """
        return self.get_channel_info(channel_name)['members']

    def channel_has_only_restricted_members(self, channel_name):
        """
        returns True if the channel only has restricted/ultra_restricted
        members, False otherwise
        """

        mids = set(self.get_channel_members_ids(channel_name))
        self.logger.debug("Current members in %s are %s", channel_name, mids)
        return mids.intersection(self.all_restricted_users)

    def get_channel_member_names(self, channel_name):
        """
        returns an array of ["@member"] for members of the channel
        """
        members = self.get_channel_members_ids(channel_name)
        return ["@" + self.users_by_id[x] for x in members]

    def get_channel_info(self, channel_name):
        """
        returns JSON with channel information.  Adds 'age' in seconds to JSON
        """
        url_template = self.url + "channels.info?token={}&channel={}"
        cid = self.get_channelid(channel_name)
        now = int(time.time())
        url = url_template.format(self.token, cid)
        ret = self.session.get(url).json()
        if ret['ok'] is not True:
            m = "Attempted to get channel info for {}, but return was {}"
            m = m.format(channel_name, ret)
            raise RuntimeError(m)
        created = ret['channel']['created']
        age = now - created
        ret['channel']['age'] = age
        return ret['channel']

    def get_all_channel_objects(self, exclude_archived=True):
        """
        return all channels
        if exclude_archived (default: True), only shows non-archived channels
        """

        url_template = self.url + "channels.list?exclude_archived={}&token={}"
        if exclude_archived:
            exclude_archived = 1
        else:
            exclude_archived = 0
        url = url_template.format(exclude_archived, self.token)
        request = self.session.get(url)
        payload = request.json()
        assert 'channels' in payload
        return payload['channels']

    def get_all_user_objects(self):
        url = self.url + "users.list?token=" + self.token
        return self.session.get(url).json()['members']

    def archive(self, channel_name):
        url_template = self.url + "channels.archive?token={}&channel={}"
        cid = self.get_channelid(channel_name)
        url = url_template.format(self.token, cid)
        request = self.session.get(url)
        payload = request.json()
        return payload

    def post_message(self, channel, message, message_type=None):
        """
        Posts a `message` into a `channel`.
        Optionally append an invisible attachment with 'fallback' set to `message_type`.

        Note: `channel` value should not be preceded with '#'.
        """
        assert channel  # not blank
        if channel[0] == '#':
            channel = channel[1:]

        post_data = {
            'token': self.token,
            'channel': channel,
            'text': message.encode('utf-8')
        }

        bot_name = self.config.bot_name
        bot_avatar_url = self.config.bot_avatar_url
        if bot_name or bot_avatar_url:
            post_data['as_user'] = False
            if bot_name:
                post_data['username'] = bot_name
            if bot_avatar_url:
                post_data['icon_url'] = bot_avatar_url

        if message_type:
            post_data['attachments'] = json.dumps([{'fallback': message_type}], encoding='utf-8')

        p = self.session.post(self.url + "chat.postMessage", data=post_data)
        return p.json()
