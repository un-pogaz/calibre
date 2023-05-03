#!/usr/bin/env python
# License: GPLv3 Copyright: 2023, Kovid Goyal <kovid at kovidgoyal.net>


import re
from qt.core import (
    QApplication, QBrush, QColor, QFont, QSyntaxHighlighter, QTextCharFormat,
    QTextCursor, QTextLayout,
)

from calibre.gui2.palette import dark_link_color, light_link_color


class MarkdownHighlighter(QSyntaxHighlighter):

    MARKDOWN_KEYS_REGEX = {
        'Bold': re.compile(r'(?<!\\)(?P<delim>\*\*)(?P<text>.+?)(?P=delim)'),
        'Italic': re.compile(r'(?<![\*\\])(?P<delim>\*)(?!\*)(?P<text>([^\*]{2,}?|[^\*]))(?<![\*\\])(?P=delim)'),
        'BoldItalic': re.compile(r'(?<!\\)(?P<delim>\*\*\*)(?P<text>([^\*]{2,}?|[^\*]))(?<!\\)(?P=delim)'),
        'uBold': re.compile(r'(?<!\\)(?P<delim>__)(?P<text>.+?)(?P=delim)'),
        'uItalic': re.compile(r'(?<![_\\])(?P<delim>_)(?!_)(?P<text>([^_]{2,}?|[^_]))(?<![_\\])(?P=delim)'),
        'uBoldItalic': re.compile(r'(?<!\\)(?P<delim>___)(?P<text>([^_]{2,}?|[^_]))(?<!\\)(?P=delim)'),
        'Link': re.compile(r'(?u)(?<![!\\]])\[.*?(?<!\\)\](\[.+?(?<!\\)\]|\(.+?(?<!\\)\))'),
        'Image': re.compile(r'(?u)(?<!\\)!\[.*?(?<!\\)\](\[.+?(?<!\\)\]|\(.+?(?<!\\)\))'),
        'LinkRef': re.compile(r'(?u)^ *\[.*?\]:[ \t]*.*$'),
        'Header': re.compile(r'(?u)^#{1,6}(.*?)$'),
        'CodeBlock': re.compile('^([ ]{4,}|\t).*'),
        'UnorderedList': re.compile(r'(?u)^\s*(\* |\+ |- )+\s*'),
        'UnorderedListStar': re.compile(r'^\s*(\* )+\s*'),
        'OrderedList': re.compile(r'(?u)^\s*(\d+\. )\s*'),
        'BlockQuote': re.compile(r'(?u)^[ ]{0,3}>+[ \t]?'),
        'CodeSpan': re.compile(r'(?<!\\)(?P<delim>`+).+?(?P=delim)'),
        'HeaderLine': re.compile(r'(?u)^(-|=)+\s*$'),
        'HR': re.compile(r'(?u)^(\s*(\*|-|_)\s*){3,}$'),
        'Html': re.compile(r'<.+?(?<!\\)>')
    }

    key_theme_maps = {
        'Bold': "bold",
        'Italic': "emphasis",
        'BoldItalic': "boldemphasis",
        'uBold': "bold",
        'uItalic': "emphasis",
        'uBoldItalic': "boldemphasis",
        'Link': "link",
        'Image': "image",
        'LinkRef': "link",
        'Header': "header",
        'HeaderLine': "header",
        'UnorderedList': "unorderedlist",
        'UnorderedListStar': "unorderedlist",
        'OrderedList': "orderedlist",
        'BlockQuote': "blockquote",
        'CodeBlock': "code",
        'CodeSpan': "code",
        'HR': "line",
        'Html': "html",
    }

    light_theme =  {
        "bold": {"font-weight":"bold"},
        "emphasis": {"font-style":"italic"},
        "boldemphasis": {"font-weight":"bold", "font-style":"italic"},
        "link": {"color":light_link_color.name(), "font-weight":"normal", "font-style":"normal"},
        "image": {"color":"#cb4b16", "font-weight":"normal", "font-style":"normal"},
        "header": {"color":"#2aa198", "font-weight":"bold", "font-style":"normal"},
        "unorderedlist": {"color":"red", "font-weight":"normal", "font-style":"normal"},
        "orderedlist": {"color":"red", "font-weight":"normal", "font-style":"normal"},
        "blockquote": {"color":"red", "font-weight":"bold", "font-style":"normal"},
        "code": {"background-color":"#dfdfdf", "font-weight":"normal", "font-style":"normal", },
        "line": {"color":"#2aa198", "font-weight":"normal", "font-style":"normal"},
        "html": {"color":"#c000c0", "font-weight":"normal", "font-style":"normal"}
    }

    dark_theme =  {
        "bold": {"font-weight":"bold"},
        "emphasis": {"font-style":"italic"},
        "boldemphasis": {"font-weight":"bold", "font-style":"italic"},
        "link": {"color":dark_link_color.name(), "font-weight":"normal", "font-style":"normal"},
        "image": {"color":"#cb4b16", "font-weight":"normal", "font-style":"normal"},
        "header": {"color":"#2aa198", "font-weight":"bold", "font-style":"normal"},
        "unorderedlist": {"color":"yellow", "font-weight":"normal", "font-style":"normal"},
        "orderedlist": {"color":"yellow", "font-weight":"normal", "font-style":"normal"},
        "blockquote": {"color":"yellow", "font-weight":"bold", "font-style":"normal"},
        "code": {"background-color":"#545454", "font-weight":"normal", "font-style":"normal"},
        "line": {"color":"#2aa198", "font-weight":"normal", "font-style":"normal"},
        "html": {"color":"#F653A6", "font-weight":"normal", "font-style":"normal"}
    }

    def __init__(self, parent):
        super().__init__(parent)
        theme = self.dark_theme if QApplication.instance().is_dark_theme else self.light_theme
        self.setTheme(theme)

    def setTheme(self, theme):
        self.theme = theme
        self.MARKDOWN_KWS_FORMAT = {}

        for k,t in self.key_theme_maps.items():
            subtheme = theme[t]
            format = QTextCharFormat()
            if 'color' in subtheme:
                format.setForeground(QBrush(QColor(subtheme['color'])))
            if 'background-color' in subtheme:
                format.setBackground(QBrush(QColor(subtheme['background-color'])))
            format.setFontWeight(QFont.Weight.Bold if subtheme.get('font-weight') == 'bold' else QFont.Weight.Normal)
            format.setFontItalic(subtheme.get('font-style') == 'italic')
            self.MARKDOWN_KWS_FORMAT[k] = format

        self.rehighlight()

    def highlightBlock(self, text):
        self.offset = 0
        self.highlightMarkdown(text)
        self.highlightHtml(text)

    def highlightMarkdown(self, text):
        cursor = QTextCursor(self.document())
        bf = cursor.blockFormat()

        #Block quotes can contain all elements so process it first, internaly process recusively and return
        if self.highlightBlockQuote(text, cursor, bf):
            return

        #If empty line no need to check for below elements just return
        if self.highlightEmptyLine(text, cursor, bf):
            return

        #If horizontal line, look at pevious line to see if its a header, process and return
        if self.highlightHorizontalLine(text, cursor, bf):
            return

        if self.highlightHeader(text, cursor, bf):
            return

        self.highlightList(text, cursor, bf)

        self.highlightBoldEmphasis(text, cursor, bf)

        self.highlightLink(text, cursor, bf)

        self.highlightImage(text, cursor, bf)

        self.highlightCodeSpan(text, cursor, bf)

        self.highlightCodeBlock(text, cursor, bf)

    def highlightBlockQuote(self, text, cursor, bf):
        found = False
        mo = re.search(self.MARKDOWN_KEYS_REGEX['BlockQuote'],text)
        if mo:
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['BlockQuote'])
            self.offset += mo.end()
            unquote = text[mo.end():]
            self.highlightMarkdown(unquote)
            found = True
        return found

    def highlightEmptyLine(self, text, cursor, bf):
        textAscii = str(text.replace('\u2029','\n'))
        if textAscii.strip():
            return False
        else:
            return True

    def highlightHorizontalLine(self, text, cursor, bf):
        found = False

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['HeaderLine'],text):
            prevBlock = self.currentBlock().previous()
            prevCursor = QTextCursor(prevBlock)
            prev = prevBlock.text()
            prevAscii = str(prev.replace('\u2029','\n'))
            if self.offset == 0 and prevAscii.strip():
                #print "Its a header"
                prevCursor.select(QTextCursor.SelectionType.LineUnderCursor)
                #prevCursor.setCharFormat(self.MARKDOWN_KWS_FORMAT['Header'])
                formatRange = QTextLayout.FormatRange()
                formatRange.format = self.MARKDOWN_KWS_FORMAT['Header']
                formatRange.length = prevCursor.block().length()
                formatRange.start = 0
                prevCursor.block().layout().setFormats([formatRange])
                self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['HeaderLine'])
                return True

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['HR'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['HR'])
            found = True
        return found

    def highlightHeader(self, text, cursor, bf):
        found = False
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['Header'],text):
            #bf.setBackground(QBrush(QColor(7,54,65)))
            #cursor.movePosition(QTextCursor.End)
            #cursor.mergeBlockFormat(bf)
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['Header'])
            found = True
        return found

    def highlightList(self, text, cursor, bf):
        found = False
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['UnorderedList'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['UnorderedList'])
            found = True

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['OrderedList'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['OrderedList'])
            found = True
        return found

    def highlightLink(self, text, cursor, bf):
        found = False
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['Link'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['Link'])
            found = True

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['LinkRef'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['LinkRef'])
            found = True
        return found

    def highlightImage(self, text, cursor, bf):
        found = False
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['Image'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['Image'])
            found = True
        return found

    def highlightCodeSpan(self, text, cursor, bf):
        found = False
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['CodeSpan'],text):
            self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['CodeSpan'])
            found = True
        return found

    def highlightBoldEmphasis(self, text, cursor, bf):
        mo = re.match(self.MARKDOWN_KEYS_REGEX['UnorderedListStar'], text)
        if mo:
            offset = mo.end()
        else:
            offset = 0
        return self._highlightBoldEmphasis(text[offset:], cursor, bf, offset, False, False)

    def _highlightBoldEmphasis(self, text, cursor, bf, offset, bold, emphasis):
        #detect and apply imbricated Bold/Emphasis
        found = False

        def apply(match, bold, emphasis):
            if bold and emphasis:
                self.setFormat(self.offset+offset+ match.start(), match.end() - match.start(), self.MARKDOWN_KWS_FORMAT['BoldItalic'])
            elif bold:
                self.setFormat(self.offset+offset+ match.start(), match.end() - match.start(), self.MARKDOWN_KWS_FORMAT['Bold'])
            elif emphasis:
                self.setFormat(self.offset+offset+ match.start(), match.end() - match.start(), self.MARKDOWN_KWS_FORMAT['Italic'])

        def recusive(match, extra_offset, bold, emphasis):
            apply(match, bold, emphasis)
            if bold and emphasis:
                return  # max deep => return, do not process extra Bold/Italic

            sub_txt = text[match.start()+extra_offset : match.end()-extra_offset]
            sub_offset = offset + extra_offset + mo.start()
            self._highlightBoldEmphasis(sub_txt, cursor, bf, sub_offset, bold, emphasis)

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['Italic'],text):
            recusive(mo, 1, bold, True)
            found = True
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['uItalic'],text):
            recusive(mo, 1, bold, True)
            found = True

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['Bold'],text):
            recusive(mo, 2, True, emphasis)
            found = True
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['uBold'],text):
            recusive(mo, 2, True, emphasis)
            found = True

        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['BoldItalic'],text):
            apply(mo, True, True)
            found = True
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['uBoldItalic'],text):
            apply(mo, True, True)
            found = True

        return found

    def highlightCodeBlock(self, text, cursor, bf):
        found = False
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['CodeBlock'],text):
            stripped = text.lstrip()
            if stripped[0] not in ('*','-','+','>') and not re.match(r'\d+\.', stripped):
                self.setFormat(self.offset+ mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['CodeBlock'])
                found = True
        return found

    def highlightHtml(self, text):
        for mo in re.finditer(self.MARKDOWN_KEYS_REGEX['Html'], text):
            self.setFormat(mo.start(), mo.end() - mo.start(), self.MARKDOWN_KWS_FORMAT['Html'])
