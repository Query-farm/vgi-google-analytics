"""Query Farm's shared Google *Desktop app* OAuth registration.

Query Farm sets these identifiers before distributing the worker. Every user
then signs in with their own Google account through the same app. Desktop apps
are public clients: this client secret is not a confidential web-server secret.
Never put user tokens here. No per-user app registration or configuration is needed.
"""

CLIENT_ID = "844240813731-2qfp67c6gfibs0jkvl47l749g3050jqb.apps.googleusercontent.com"
CLIENT_SECRET = "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"
