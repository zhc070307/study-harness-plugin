"""Native PDF operations using permissively licensed PDFium, without Poppler."""
from io import BytesIO
from contextlib import closing
import os
from pathlib import Path
import subprocess
import tempfile

def page_texts(source):
    """Yield every PDF page, including empty pages, without a whole-book text limit."""
    if os.environ.get('STUDY_NATIVE') == '1' or os.environ.get('STUDY_PDFIUM') == '1':
        import pypdfium2 as pdfium
        with closing(pdfium.PdfDocument(str(source))) as document:
            for index in range(len(document)):
                with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                    yield index + 1, textpage.get_text_bounded()
        return
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'pages.txt'
        subprocess.run(['pdftotext', '-layout', '-enc', 'UTF-8', str(source), str(target)],
                       check=True, capture_output=True, timeout=90)
        number, pending = 1, ''
        with target.open(encoding='utf-8') as stream:
            while chunk := stream.read(65536):
                parts = (pending + chunk).split('\f')
                for content in parts[:-1]:
                    yield number, content
                    number += 1
                pending = parts[-1]
        if pending:
            yield number, pending

def text(source):
    import pypdfium2 as pdfium
    with closing(pdfium.PdfDocument(str(source))) as document:
        result=[]
        for index in range(len(document)):
            with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                result.append(textpage.get_text_bounded())
        return '\n'.join(result)

def pages(source):
    import pypdfium2 as pdfium
    with closing(pdfium.PdfDocument(str(source))) as document:return len(document)

def image(source, number):
    import pypdfium2 as pdfium
    with closing(pdfium.PdfDocument(str(source))) as document:
        if not 1<=number<=len(document):raise ValueError('页码超出文档范围')
        with closing(document[number-1]) as page:
            scale=1400/max(page.get_size())
            with closing(page.render(scale=scale)) as bitmap:
                output=BytesIO();bitmap.to_pil().save(output,format='PNG');return output.getvalue()
