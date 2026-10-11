# Third-party notices

This independent plugin selects learning-domain and bridge source from
[Study Agent](https://github.com/zhc070307/study-agent), commit
`14e4dbba1ad1c9c31a17d412e09ea3d1faaad1ff`, distributed under MIT.
The root LICENSE is retained. SOURCE.json records the selected components.

The optional transcription pipeline selects LocalTranscribe source under
Apache-2.0. Its complete license, provenance and modification notice remain at
`packages/transcription/LICENSE`, `packages/transcription/NOTICE` and
`packages/transcription/SOURCE.json`.

DeepSeek Harness and Cordis are external peer dependencies; their implementation
is not bundled into this plugin. They retain their own licenses. The Harness
version used for integration tests is 0.2.1-alpha.2.

Python dependencies are installed separately: pypdfium2 (Apache-2.0/BSD-3-Clause,
with PDFium's own BSD-style component licenses), Pillow (HPND), icalendar
(BSD-2-Clause), and tzdata (Apache-2.0; IANA data is public domain).
Optional transcription dependencies and downloaded model weights retain their
individual upstream licenses; installing the plugin does not download them.

The Apple Calendar helper uses macOS EventKit. The original read-only helper is
retained; the structured read/write helper is an independent Study Agent addition.
