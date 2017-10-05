"""
Classes for Repo providers

Subclass the base class, ``RepoProvider``, to support different version
control services and providers.

"""
from datetime import datetime
import json
import os
import time

from tornado import gen
from tornado.httpclient import AsyncHTTPClient, HTTPError
from tornado.httputil import url_concat

from traitlets import Dict, Unicode, default
from traitlets.config import LoggingConfigurable


class RepoProvider(LoggingConfigurable):
    """Base class for a repo provider"""
    name = Unicode(
        help="""
        Descriptive human readable name of this repo provider.
        """
    )

    spec = Unicode(
        help="""
        The spec for this builder to parse
        """
    )

    @gen.coroutine
    def get_resolved_ref(self):
        raise NotImplementedError("Must be overridden in child class")

    def get_repo_url(self):
        raise NotImplementedError("Must be overridden in the child class")

    def get_build_slug(self):
        raise NotImplementedError("Must be overriden in the child class")


class GitHubRepoProvider(RepoProvider):
    """Repo provider for the GitHub service"""
    name = Unicode('GitHub')

    client_id = Unicode(config=True,
        help="""GitHub client id for authentication with the GitHub API

        For use with client_secret.
        Loaded from GITHUB_CLIENT_ID env by default.
        """
    )
    @default('client_id')
    def _client_id_default(self):
        return os.getenv('GITHUB_CLIENT_ID', '')

    client_secret = Unicode(config=True,
        help="""GitHub client secret for authentication with the GitHub API

        For use with client_id.
        Loaded from GITHUB_CLIENT_SECRET env by default.
        """
    )
    @default('client_secret')
    def _client_secret_default(self):
        return os.getenv('GITHUB_CLIENT_SECRET', '')

    access_token = Unicode(config=True,
        help="""GitHub access token for authentication with the GitHub API

        Loaded from GITHUB_ACCESS_TOKEN env by default.
        """
    )
    @default('access_token')
    def _access_token_default(self):
        return os.getenv('GITHUB_ACCESS_TOKEN', '')

    auth = Dict(
        help="""Auth parameters for the GitHub API access
    
        Populated from client_id, client_secret, access_token.
    """
    )
    @default('auth')
    def _default_auth(self):
        auth = {}
        for key in ('client_id', 'client_secret', 'access_token'):
            value = getattr(self, key)
            if value:
                auth[key] = value
        return auth

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        spec_parts = self.spec.split('/')
        if len(spec_parts) != 3:
            msg = 'Spec is not of the form "user/repo/ref", provided: "{spec}".'.format(spec=self.spec)
            if len(spec_parts) == 2 and spec_parts[-1] != 'master':
                msg += ' Did you mean "{spec}/master"?'.format(spec=self.spec)
            raise ValueError(msg)

        self.user, self.repo, self.unresolved_ref = spec_parts
        if self.repo.endswith('.git'):
            self.repo = self.repo[:len(self.repo) - 4]

    def get_repo_url(self):
        return "https://github.com/{user}/{repo}".format(user=self.user, repo=self.repo)

    @gen.coroutine
    def get_resolved_ref(self):
        if hasattr(self, 'resolved_ref'):
            return self.resolved_ref

        client = AsyncHTTPClient()
        api_url = "https://api.github.com/repos/{user}/{repo}/commits/{ref}".format(
            user=self.user, repo=self.repo, ref=self.unresolved_ref
        )
        self.log.debug("Fetching %s", api_url)

        if self.auth:
            # Add auth params. After logging!
            api_url = url_concat(api_url, self.auth)

        try:
            resp = yield client.fetch(api_url, user_agent="BinderHub")
        except HTTPError as e:
            if (
                e.code == 403
                and e.response
                and e.response.headers.get('x-ratelimit-remaining') == '0'
            ):
                rate_limit = e.response.headers['x-ratelimit-limit']
                reset_timestamp = int(e.response.headers['x-ratelimit-reset'])
                reset_seconds = int(reset_timestamp - time.time())
                self.log.error(
                    "GitHub Rate limit ({limit}) exceeded. Reset in {seconds} seconds ({date})".format(
                        limit=rate_limit,
                        seconds=reset_seconds,
                        date=datetime.fromtimestamp(reset_timestamp),
                    )
                )
                # round to reset plus five minutes
                minutes_until_reset = 5 * (1 + (reset_seconds // 60 // 5))

                raise ValueError("GitHub rate limit exceeded. Try again in %i minutes."
                    % minutes_until_reset
                )
            elif e.code == 404:
                return None
            else:
                raise

        ref_info = json.loads(resp.body.decode('utf-8'))
        if 'sha' not in ref_info:
            # TODO: Figure out if we should raise an exception instead?
            return None
        self.resolved_ref = ref_info['sha']
        return self.resolved_ref

    def get_build_slug(self):
        return '{user}-{repo}'.format(user=self.user, repo=self.repo)
