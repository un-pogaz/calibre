#!/usr/bin/env python


import importlib
import os
import sys
import time

from qt.core import QIcon

from calibre.constants import EDITOR_APP_UID, islinux, ismacos
from calibre.ebooks.oeb.polish.check.css import shutdown as shutdown_css_check_pool
from calibre.gui2 import Application, decouple, set_gui_prefs, setup_gui_option_parser
from calibre.ptempfile import reset_base_dir
from calibre.utils.config import OptionParser

__license__ = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'


def option_parser():
    parser = OptionParser(
        _(
            '''\
%prog [opts] [path_to_ebook] [name_of_file_inside_book ...]

Launch the calibre Edit book tool. You can optionally also specify the names of
files inside the book which will be opened for editing automatically.
'''
        )
    )
    setup_gui_option_parser(parser)
    parser.add_option('--select-text', default=None, help=_('The text to select in the book when it is opened for editing'))
    return parser


def gui_main(path=None, notify=None):
    _run(['ebook-edit', path], notify=notify)


def _run(args, notify=None):
    from calibre.utils.webengine import setup_fake_protocol
    # Ensure we can continue to function if GUI is closed
    os.environ.pop('CALIBRE_WORKER_TEMP_DIR', None)
    reset_base_dir()
    setup_fake_protocol()

    # The following two lines are needed to prevent circular imports causing
    # errors during initialization of plugins that use the polish container
    # infrastructure.
    importlib.import_module('calibre.customize.ui')
    from calibre.gui2.tweak_book import tprefs
    from calibre.gui2.tweak_book.ui import Main

    parser = option_parser()
    opts, args = parser.parse_args(args)
    decouple('edit-book-'), set_gui_prefs(tprefs)
    override = 'calibre-ebook-edit' if islinux else None
    app = Application(args, override_program_name=override, color_prefs=tprefs, windows_app_uid=EDITOR_APP_UID)
    from calibre.utils.webengine import setup_default_profile
    setup_default_profile()
    app.load_builtin_fonts()
    if not ismacos:
        app.setWindowIcon(QIcon.ic('tweak.png'))
    main = Main(opts, notify=notify)
    main.set_exception_handler()
    main.show()
    app.shutdown_signal_received.connect(main.boss.quit)
    if len(args) > 1:
        main.boss.open_book(args[1], edit_file=args[2:], clear_notify_data=False, search_text=opts.select_text)
    else:
        paths = app.get_pending_file_open_events()
        if paths:
            if len(paths) > 1:
                from .boss import open_path_in_new_editor_instance
                for path in paths[1:]:
                    try:
                        open_path_in_new_editor_instance(path)
                    except Exception:
                        import traceback
                        traceback.print_exc()
            main.boss.open_book(paths[0])
    app.file_event_hook = main.boss.open_book
    app.exec()
    # Ensure that the parse worker has quit so that temp files can be deleted
    # on windows
    st = time.time()
    from calibre.gui2.tweak_book.preview import parse_worker
    while parse_worker.is_alive() and time.time() - st < 120:
        time.sleep(0.1)


def main(args=sys.argv):
    try:
        _run(args)
    finally:
        shutdown_css_check_pool()


if __name__ == '__main__':
    main()
