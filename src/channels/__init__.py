"""One module per way in.

A channel owns three things and nothing else: how a request authenticates, how
a Message is rendered for that audience, and how the answer gets back. The
handlers behind them are shared and identical.
"""
