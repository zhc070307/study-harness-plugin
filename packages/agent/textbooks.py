"""Versioned, per-page textbook lookup. Original PDFs remain the source of truth."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

import pdf_tools
from study_agent import db
from agreements import read_only


def integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(label + '必须是有效整数')
    return value


def schema(conn):
    # Indexed page text and maps participate in the existing semester SQLite backup.
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS textbook_documents (
            sha256 TEXT PRIMARY KEY, pages INTEGER NOT NULL, text_pages INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS textbook_books (
            file TEXT PRIMARY KEY, sha256 TEXT NOT NULL, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS textbook_pages (
            id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, pdf_page INTEGER NOT NULL,
            text TEXT NOT NULL, UNIQUE(sha256,pdf_page));
        CREATE VIRTUAL TABLE IF NOT EXISTS textbook_fts USING fts5(
            text, content='textbook_pages', content_rowid='id', tokenize='unicode61');
        CREATE TRIGGER IF NOT EXISTS textbook_insert AFTER INSERT ON textbook_pages BEGIN
            INSERT INTO textbook_fts(rowid,text) VALUES (new.id,new.text);
        END;
    ''')


def metadata(args, previous, pages):
    result = dict(previous)
    for key in ('title', 'edition'):
        if key in args:
            value = args[key]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError('教材名称与版本应填写实际文字，未知时留空')
            result[key] = value.strip() if value is not None else None
    for key in ('chapters', 'page_map'):
        if key in args:
            if not isinstance(args[key], list):
                raise ValueError('章节与页码映射必须是列表')
            result[key] = args[key]
        result.setdefault(key, [])
    chapters = []
    for item in result['chapters']:
        if not isinstance(item, dict):
            raise ValueError('章节记录必须是对象')
        start = integer(item.get('pdf_start'), '章节起始 PDF 页码')
        end = integer(item.get('pdf_end'), '章节结束 PDF 页码')
        if end < start or end > pages:
            raise ValueError('章节范围超出教材页数')
        title = item.get('title', '')
        number = item.get('number', '')
        if not isinstance(title, str) or not title.strip() or not isinstance(number, str):
            raise ValueError('请填写章节标题；章节编号使用实际文字')
        chapter = dict(number=number.strip(), title=title.strip(), pdf_start=start, pdf_end=end)
        if chapter in chapters:
            raise ValueError('章节记录重复')
        chapters.append(chapter)
    mappings, covered = [], set()
    for item in result['page_map']:
        if not isinstance(item, dict):
            raise ValueError('页码映射必须是对象')
        start = integer(item.get('pdf_start'), '起始 PDF 页码')
        end = integer(item.get('pdf_end', start), '结束 PDF 页码')
        if end < start or end > pages:
            raise ValueError('页码映射超出教材页数')
        if covered.intersection(range(start, end + 1)):
            raise ValueError('PDF 页码映射重叠，请核对实际原页')
        covered.update(range(start, end + 1))
        if 'book_start' in item:
            label = integer(item['book_start'], '纸书起始页码', minimum=0)
            mapping = dict(pdf_start=start, pdf_end=end, book_start=label)
        else:
            label = item.get('label')
            if end != start or not isinstance(label, str) or not label.strip():
                raise ValueError('非数字纸书页码需逐页提供实际 label')
            mapping = dict(pdf_start=start, pdf_end=end, label=label.strip())
        mappings.append(mapping)
    result.update(chapters=chapters, page_map=mappings)
    return result


class Textbooks:
    def __init__(self, store, args):
        self.store, self.args, self.conn = store, args, store.conn
        self.file = store.file(args['file'])
        if self.file.get('removed'):
            raise ValueError('教材已移除，请恢复后再检索')
        if self.file['mime'] != 'application/pdf':
            raise ValueError('教材检索目前支持 PDF，请选择原教材 PDF')
        self.exists = bool(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='textbook_documents'").fetchone())

    def book(self):
        if not self.exists:
            return None
        row = self.conn.execute('SELECT * FROM textbook_books WHERE file=?', (self.file['id'],)).fetchone()
        return dict(row) if row else None

    def document(self):
        if not self.exists:
            return None
        row = self.conn.execute('SELECT * FROM textbook_documents WHERE sha256=?', (self.file['sha256'],)).fetchone()
        return dict(row) if row else None

    def source(self, info):
        file = self.file
        return dict(file=file['id'], name=file['name'], title=info.get('title') or file['name'],
                    edition=info.get('edition'), sha256=file['sha256'],
                    path=str(self.store.blobs / file['sha256']), course=file.get('course'),
                    course_path=file.get('course_path'), url=self.store.public(file)['url'])

    def status(self):
        book, document = self.book(), self.document()
        ready = bool(book and document and book['sha256'] == self.file['sha256'])
        info = json.loads(book['metadata']) if ready else {}
        result = dict(ready=ready, source=self.source(info),
                      reason=None if ready else ('source_changed' if book else 'not_indexed'))
        if ready:
            empty = self.conn.execute('SELECT pdf_page FROM textbook_pages WHERE sha256=? AND trim(text)=\'\' ORDER BY pdf_page LIMIT 30', (self.file['sha256'],)).fetchall()
            result.update(pages=document['pages'], searchable_pages=document['text_pages'],
                          chapters=info['chapters'], page_map=info['page_map'],
                          empty_text_pages=[row[0] for row in empty],
                          empty_text_page_count=document['pages'] - document['text_pages'],
                          warning='部分页面没有可检索文字，可能是空白或扫描页；需要查看原页，当前不自动 OCR。' if document['text_pages'] < document['pages'] else None)
        return result

    def index(self):
        if read_only(self.store.root):
            raise ValueError('这个学期已归档；已有索引可查询，重新开始后才能建立或修改索引')
        if self.args.get('expected') != self.file['sha256']:
            raise ValueError('教材版本已变化或未确认，请重新读取文件后建立索引')
        path = self.store.blobs / self.file['sha256']
        with path.open('rb') as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
            if digest.hexdigest() != self.file['sha256']:
                raise ValueError('教材原件与保存版本不一致，请恢复原件')
        schema(self.conn)
        self.exists = True
        with db.atomic(self.conn, bump_revision=False):
            current = self.store.file(self.file['id'])
            if current['sha256'] != self.file['sha256'] or current.get('removed'):
                raise ValueError('教材在处理期间已变化，请重新读取文件')
            self.file = current
            document = self.document()
            reused = document is not None
            if not reused:
                count = text_pages = 0
                for page, content in pdf_tools.page_texts(path):
                    count += 1
                    if page != count:
                        raise ValueError('PDF 页面提取不完整，未保存此次索引')
                    content = content.replace('\x00', '')
                    text_pages += bool(content.strip())
                    self.conn.execute('INSERT INTO textbook_pages(sha256,pdf_page,text) VALUES (?,?,?)',
                                      (self.file['sha256'], page, content))
                if count == 0:
                    raise ValueError('无法读取教材页面，未保存此次索引')
                self.conn.execute('INSERT INTO textbook_documents VALUES (?,?,?)',
                                  (self.file['sha256'], count, text_pages))
                document = self.document()
            book = self.book()
            previous = json.loads(book['metadata']) if book and book['sha256'] == self.file['sha256'] else {}
            info = metadata(self.args, previous, document['pages'])
            self.conn.execute('INSERT OR REPLACE INTO textbook_books VALUES (?,?,?)',
                              (self.file['id'], self.file['sha256'], json.dumps(info, ensure_ascii=False)))
        return dict(self.status(), reused_page_text=reused)

    def ready(self):
        state = self.status()
        if not state['ready']:
            raise ValueError('教材尚未建立当前版本的索引，请先 index；不能使用旧版本的页码映射')
        return state

    def location(self, page, state):
        info = json.loads(self.book()['metadata'])
        label = None
        for mapping in info['page_map']:
            if mapping['pdf_start'] <= page <= mapping['pdf_end']:
                label = str(mapping['book_start'] + page - mapping['pdf_start']) if 'book_start' in mapping else mapping['label']
                break
        chapters = [chapter for chapter in info['chapters'] if chapter['pdf_start'] <= page <= chapter['pdf_end']]
        chapters.sort(key=lambda chapter: chapter['pdf_end'] - chapter['pdf_start'])
        return dict(pdf_page=page, book_page=label, chapters=chapters,
                    citation=dict(source=state['source'], pdf_page=page, book_page=label,
                                  url=state['source']['url'] + '#page=' + str(page)))

    def search(self):
        state = self.ready()
        query = self.args.get('query', '')
        if not isinstance(query, str):
            raise ValueError('关键词必须是文字')
        # Literal words only: FTS operators and SQL are never inferred from user text.
        cjk = r'\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f'
        terms = re.findall('[' + cjk + ']+|[^\\W_' + cjk + ']+', query, flags=re.UNICODE)
        if query.strip() and not terms:
            raise ValueError('请提供有效关键词')
        filters, values = ['p.sha256=?'], [self.file['sha256']]
        start = integer(self.args.get('pdf_start', 1), '起始 PDF 页码')
        end = integer(self.args.get('pdf_end', state['pages']), '结束 PDF 页码')
        if start > end or end > state['pages']:
            raise ValueError('检索范围超出教材页数')
        filters.append('p.pdf_page BETWEEN ? AND ?')
        values.extend([start, end])
        chapter = self.args.get('chapter')
        if chapter is not None:
            if not isinstance(chapter, str) or not chapter.strip():
                raise ValueError('章节需使用实际编号或标题')
            matches = [item for item in state['chapters'] if chapter in (item['number'], item['title'])]
            if not matches:
                raise ValueError('没有此章节的已核对映射，请先查看目录和原页再补充 chapters')
            filters.append('(' + ' OR '.join('p.pdf_page BETWEEN ? AND ?' for _ in matches) + ')')
            values.extend(number for item in matches for number in (item['pdf_start'], item['pdf_end']))
        label = self.args.get('book_page')
        if label is not None:
            if not isinstance(label, str) or not label.strip():
                raise ValueError('纸书页码使用实际文字，例如 563 或 iv')
            mapped = []
            for item in state['page_map']:
                if 'book_start' in item and label.isdecimal():
                    page = item['pdf_start'] + int(label) - item['book_start']
                    if item['pdf_start'] <= page <= item['pdf_end']:
                        mapped.append(page)
                elif item.get('label') == label:
                    mapped.append(item['pdf_start'])
            if not mapped:
                raise ValueError('没有此纸书页码的已核对映射，不把纸书页码当成 PDF 页码')
            filters.append('p.pdf_page IN (' + ','.join('?' for _ in mapped) + ')')
            values.extend(mapped)
        limit = integer(self.args.get('limit', 8), '结果数量')
        offset = integer(self.args.get('offset', 0), '结果起点', minimum=0)
        if limit > 30:
            raise ValueError('每次最多返回 30 页，可用 offset 继续查询')
        cjk_terms = [term for term in terms if re.search(r'[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]', term)]
        word_terms = [term for term in terms if term not in cjk_terms]
        for term in cjk_terms:
            filters.append("instr(replace(replace(replace(replace(p.text,char(13),''),char(10),''),char(9),''),' ',''),?)>0")
            values.append(term)
        if word_terms:
            table = 'textbook_fts JOIN textbook_pages p ON p.id=textbook_fts.rowid'
            snippet = "snippet(textbook_fts,0,'[',']',' … ',48)"
            filters.append('textbook_fts MATCH ?')
            values.append(' AND '.join('"' + term + '"' for term in word_terms))
            order = 'bm25(textbook_fts),p.pdf_page'
        else:
            if not cjk_terms and chapter is None and label is None and 'pdf_start' not in self.args and 'pdf_end' not in self.args:
                raise ValueError('请提供关键词、章节或页码范围')
            table, snippet, order = 'textbook_pages p', 'substr(p.text,1,350)', 'p.pdf_page'
        try:
            rows = self.conn.execute('SELECT p.pdf_page,' + snippet + ' AS snippet FROM ' + table +
                                     ' WHERE ' + ' AND '.join(filters) + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?',
                                     [*values, limit + 1, offset]).fetchall()
        except sqlite3.OperationalError as error:
            raise ValueError('教材关键词检索失败，请核对索引与查询') from error
        return dict(source=state['source'], query=query,
                    results=[dict(self.location(row['pdf_page'], state), snippet=row['snippet']) for row in rows[:limit]],
                    next_offset=offset + limit if len(rows) > limit else None,
                    warning=state['warning'], note='结果是定位线索；阅读原页核对题面、公式与图形。未命中不证明书中没有相关内容。')

    def read(self):
        state = self.ready()
        if self.args.get('expected', self.file['sha256']) != self.file['sha256']:
            raise ValueError('教材版本已变化，请重新查询')
        page = integer(self.args.get('pdf_page'), 'PDF 页码')
        if page > state['pages']:
            raise ValueError('页码超出教材范围')
        offset = integer(self.args.get('offset', 0), '文字起点', minimum=0)
        limit = integer(self.args.get('limit', 20000), '文字长度')
        if limit > 50000:
            raise ValueError('单次文字最多 50000 字，可用 offset 继续读取')
        content = self.conn.execute('SELECT text FROM textbook_pages WHERE sha256=? AND pdf_page=?',
                                    (self.file['sha256'], page)).fetchone()[0]
        if offset > len(content):
            raise ValueError('文字起点超出页面内容')
        end = min(offset + limit, len(content))
        return dict(self.location(page, state), text=content[offset:end], characters=len(content),
                    next_offset=end if end < len(content) else None,
                    warning=None if content.strip() else '本页没有可提取文字，请查看原 PDF 页面。')


def perform(store, args):
    action = args.get('action')
    if action not in ('status', 'index', 'search', 'read'):
        raise ValueError('教材操作应为 status、index、search 或 read')
    return getattr(Textbooks(store, args), action)()
