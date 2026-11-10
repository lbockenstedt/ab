"""A running AB keeps the templates it loaded. Self-update rewrites templates/
before the restart; if the old process hot-reloaded them it would 500 on new
globals it never registered ("'feed_counts' is undefined")."""

import os

import main


def test_old_process_keeps_its_loaded_template_after_disk_update():
    env = main.templates.env
    first = env.get_template("index.html")
    path = first.filename
    st = os.stat(path)
    try:
        os.utime(path, (st.st_atime, st.st_mtime + 60))  # the self-update rewrote it
        assert env.get_template("index.html") is first
    finally:
        os.utime(path, (st.st_atime, st.st_mtime))
