#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2010, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import copy
import os
from functools import partial

from qt.core import QAbstractItemView, QApplication, QIcon, QMenu, Qt, QTableWidgetItem

from calibre.constants import config_dir
from calibre.db.constants import TEMPLATE_ICON_INDICATOR
from calibre.gui2 import gprefs
from calibre.gui2.preferences import ConfigTabWidget, ConfigWidgetBase
from calibre.gui2.preferences.look_feel_tabs.tb_icon_rules_ui import Ui_Form

CATEGORY_COLUMN = 0
VALUE_COLUMN = 1
ICON_COLUMN = 2
FOR_CHILDREN_COLUMN = 3
DELECTED_COLUMN = 4


class CategoryTableWidgetItem(QTableWidgetItem):

    def __init__(self, txt):
        super().__init__(txt)
        self._is_deleted = False

    @property
    def is_deleted(self):
        return self._is_deleted

    @is_deleted.setter
    def is_deleted(self, to_what):
        self._is_deleted = to_what


class TbIconRulesTab(ConfigTabWidget, Ui_Form):

    def genesis(self, gui):
        self.gui = gui
        r = self.register
        r('tag_browser_show_category_icons', gprefs)
        r('tag_browser_show_value_icons', gprefs)

        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rules_table.setColumnCount(4)
        self.rules_table.setHorizontalHeaderLabels((_('Category'), _('Value'), _('Icon file or template'),
                                        _('Use for children')))
        self.rules_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.rules_table.customContextMenuRequested.connect(self.show_context_menu)

        # Capture clicks on the horizontal header to sort the table columns
        hh = self.rules_table.horizontalHeader()
        hh.sectionResized.connect(self.table_column_resized)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self.do_sort)
        hh.setSortIndicatorShown(True)

        v = gprefs['tags_browser_value_icons']
        row = 0
        for category,vdict in v.items():
            for value in vdict:
                self.rules_table.setRowCount(row + 1)
                d = v[category][value]
                self.rules_table.setItem(row, 0, CategoryTableWidgetItem(category))
                self.rules_table.setItem(row, 1, QTableWidgetItem(value))
                self.rules_table.setItem(row, 2, QTableWidgetItem(d[0]))
                if value == TEMPLATE_ICON_INDICATOR:
                    txt = ''
                else:
                    txt = _('Yes') if d[1] else _('No')
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter|Qt.AlignmentFlag.AlignVCenter)
                self.rules_table.setItem(row, 3, item)
                row += 1

        self.category_order = 1
        self.value_order = 1
        self.icon_order = 0
        self.for_children_order = 0
        self.do_sort(VALUE_COLUMN)
        self.do_sort(CATEGORY_COLUMN)

        try:
            self.table_column_widths = gprefs.get('tag_browser_rules_dialog_table_widths', None)
        except Exception:
            pass

    def show_context_menu(self, point):
        clicked_item = self.rules_table.itemAt(point)
        item = self.rules_table.item(clicked_item.row(), CATEGORY_COLUMN)
        m = QMenu(self)
        ac = m.addAction(_('Delete this rule'), partial(self.context_menu_handler, 'delete', item))
        ac.setEnabled(not item.is_deleted)
        ac = m.addAction(_('Undo delete'), partial(self.context_menu_handler, 'undelete', item))
        ac.setEnabled(item.is_deleted)
        m.addSeparator()
        m.addAction(_('Copy'), partial(self.context_menu_handler, 'copy', clicked_item))
        m.exec(self.rules_table.viewport().mapToGlobal(point))

    def context_menu_handler(self, action, item):
        if action == 'copy':
            QApplication.clipboard().setText(item.text())
            return
        item.setIcon(QIcon.ic('trash.png') if action == 'delete' else QIcon())
        item.is_deleted = action == 'delete'
        self.changed_signal.emit()

    def table_column_resized(self, col, old, new):
        self.table_column_widths = []
        for c in range(0, self.rules_table.columnCount()):
            self.table_column_widths.append(self.rules_table.columnWidth(c))
        gprefs['tag_browser_rules_dialog_table_widths'] = self.table_column_widths

    def resizeEvent(self, *args):
        super().resizeEvent(*args)
        if self.table_column_widths is not None:
            for c,w in enumerate(self.table_column_widths):
                self.rules_table.setColumnWidth(c, w)
        else:
            # The vertical scroll bar might not be rendered, so might not yet
            # have a width. Assume 25. Not a problem because user-changed column
            # widths will be remembered.
            w = self.tb_icon_rules_groupbox.width() - 25 - self.rules_table.verticalHeader().width()
            w //= self.rules_table.columnCount()
            for c in range(0, self.rules_table.columnCount()):
                self.rules_table.setColumnWidth(c, w)
                self.table_column_widths.append(self.rules_table.columnWidth(c))
        gprefs['tag_browser_rules_dialog_table_widths'] = self.table_column_widths

    def do_sort(self, section):
        if section == CATEGORY_COLUMN:
            self.category_order = 1 - self.category_order
            self.rules_table.sortByColumn(CATEGORY_COLUMN, Qt.SortOrder(self.category_order))
        elif section == VALUE_COLUMN:
            self.value_order = 1 - self.value_order
            self.rules_table.sortByColumn(VALUE_COLUMN, Qt.SortOrder(self.value_order))
        elif section == ICON_COLUMN:
            self.icon_order = 1 - self.icon_order
            self.rules_table.sortByColumn(ICON_COLUMN, Qt.SortOrder(self.icon_order))
        elif section == FOR_CHILDREN_COLUMN:
            self.for_children_order = 1 - self.for_children_order
            self.rules_table.sortByColumn(FOR_CHILDREN_COLUMN, Qt.SortOrder(self.for_children_order))

    def commit(self):
        rr = ConfigWidgetBase.commit(self)
        v = copy.deepcopy(gprefs['tags_browser_value_icons'])
        for r in range(0, self.rules_table.rowCount()):
            cat_item = self.rules_table.item(r, CATEGORY_COLUMN)
            if cat_item.is_deleted:
                val = self.rules_table.item(r, VALUE_COLUMN).text()
                if val != TEMPLATE_ICON_INDICATOR:
                    icon_file = self.rules_table.item(r, ICON_COLUMN).text()
                    path = os.path.join(config_dir, 'tb_icons', icon_file)
                    try:
                        os.remove(path)
                    except:
                        pass
                v[cat_item.text()].pop(val, None)
        # Remove categories with no rules
        for category in list(v.keys()):
            if len(v[category]) == 0:
                v.pop(category, None)
        gprefs['tags_browser_value_icons'] = v
        return rr
