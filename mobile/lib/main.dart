// AutoMech — мобильный клиент (MVP-скелет, собирается стандартным flutter run).
// Экрана два: поиск (с фильтрами и ИИ-ответом) и карточка кейса.
// API-адрес поменяй на свой (Oracle VM / туннель).
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

const apiBase = String.fromEnvironment('API_BASE',
    defaultValue: 'http://10.0.2.2:8000'); // Android-эмулятор -> localhost хоста
const apiKey = String.fromEnvironment('API_KEY', defaultValue: '');

Map<String, String> get _headers => {
      'Content-Type': 'application/json',
      if (apiKey.isNotEmpty) 'X-Api-Key': apiKey,
    };

void main() => runApp(const AutoMechApp());

class AutoMechApp extends StatelessWidget {
  const AutoMechApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'AutoMech',
        theme: ThemeData(colorSchemeSeed: Colors.blue, useMaterial3: true),
        darkTheme: ThemeData(
            colorSchemeSeed: Colors.blue,
            brightness: Brightness.dark,
            useMaterial3: true),
        home: const SearchScreen(),
      );
}

class SearchScreen extends StatefulWidget {
  const SearchScreen({super.key});
  @override
  State<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends State<SearchScreen> {
  final _q = TextEditingController();
  final _make = TextEditingController();
  final _model = TextEditingController();
  final _dtc = TextEditingController();
  List<dynamic> _results = [];
  String _answer = '';
  bool _busy = false;

  Uri _searchUri() => Uri.parse('$apiBase/search').replace(queryParameters: {
        'q': _q.text,
        if (_make.text.isNotEmpty) 'make': _make.text,
        if (_model.text.isNotEmpty) 'model': _model.text,
        if (_dtc.text.isNotEmpty) 'dtc': _dtc.text,
      });

  Future<void> _search() async {
    if (_q.text.trim().length < 2) return;
    setState(() => _busy = true);
    try {
      final r = await http.get(_searchUri(), headers: _headers);
      final data = jsonDecode(utf8.decode(r.bodyBytes));
      setState(() {
        _results = data['results'] ?? [];
        _answer = '';
      });
    } finally {
      setState(() => _busy = false);
    }
  }

  Future<void> _ask() async {
    if (_q.text.trim().length < 2) return;
    setState(() {
      _busy = true;
      _answer = 'ИИ анализирует кейсы…';
    });
    try {
      final r = await http.post(Uri.parse('$apiBase/answer'),
          headers: _headers,
          body: jsonEncode({
            'q': _q.text,
            'filters': {'make': _make.text, 'model': _model.text, 'dtc': _dtc.text},
          }));
      final data = jsonDecode(utf8.decode(r.bodyBytes));
      setState(() {
        _answer = data['answer'] ?? data['note'] ?? 'нет ответа';
        _results = data['results'] ?? _results;
      });
    } finally {
      setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('🔧 AutoMech')),
      body: Column(children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: Column(children: [
            TextField(
              controller: _q,
              decoration: const InputDecoration(
                  labelText: 'Симптом',
                  hintText: 'дизель не заводится, стартер щёлкает…',
                  border: OutlineInputBorder()),
              onSubmitted: (_) => _search(),
            ),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(child: TextField(controller: _make,
                  decoration: const InputDecoration(labelText: 'Марка', isDense: true))),
              const SizedBox(width: 8),
              Expanded(child: TextField(controller: _model,
                  decoration: const InputDecoration(labelText: 'Модель', isDense: true))),
              const SizedBox(width: 8),
              Expanded(child: TextField(controller: _dtc,
                  decoration: const InputDecoration(labelText: 'DTC', isDense: true))),
            ]),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(
                  child: FilledButton.icon(
                      onPressed: _busy ? null : _search,
                      icon: const Icon(Icons.search),
                      label: const Text('Искать'))),
              const SizedBox(width: 8),
              Expanded(
                  child: FilledButton.tonalIcon(
                      onPressed: _busy ? null : _ask,
                      icon: const Icon(Icons.auto_awesome),
                      label: const Text('Спросить ИИ'))),
            ]),
          ]),
        ),
        if (_busy) const LinearProgressIndicator(),
        if (_answer.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Card(
                child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(_answer))),
          ),
        Expanded(
          child: ListView.builder(
            itemCount: _results.length,
            itemBuilder: (ctx, i) {
              final p = _results[i]['payload'] as Map<String, dynamic>;
              return Card(
                margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                child: ListTile(
                  title: Text(p['title'] ?? '(без названия)',
                      maxLines: 2, overflow: TextOverflow.ellipsis),
                  subtitle: Text(
                      [
                        [p['make'], p['model']].where((e) => (e ?? '').isNotEmpty).join(' '),
                        p['system'] ?? '',
                        (p['dtc_codes'] as List?)?.join(' ') ?? '',
                      ].where((e) => e.isNotEmpty).join(' | '),
                      maxLines: 2),
                  trailing: Text('${_results[i]['score']}'),
                  onTap: () => Navigator.push(
                      ctx,
                      MaterialPageRoute(
                          builder: (_) => CaseScreen(caseId: p['id'], title: p['title'] ?? ''))),
                ),
              );
            },
          ),
        ),
      ]),
    );
  }
}

class CaseScreen extends StatefulWidget {
  const CaseScreen({super.key, required this.caseId, required this.title});
  final String caseId;
  final String title;
  @override
  State<CaseScreen> createState() => _CaseScreenState();
}

class _CaseScreenState extends State<CaseScreen> {
  Map<String, dynamic>? _case;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final r = await http.get(Uri.parse('$apiBase/case/${widget.caseId}'),
        headers: _headers);
    setState(() => _case = jsonDecode(utf8.decode(r.bodyBytes)));
  }

  Future<void> _openSource([int? ts]) async {
    final url = _case?['source']?['url'] ?? '';
    if (url.isEmpty) return;
    final uri = Uri.parse(ts != null && ts > 0 ? '$url&t=${ts}s' : url);
    await launchUrl(uri, mode: LaunchMode.externalApplication);
  }

  Widget _section(String title, List<Widget> children) => children.isEmpty
      ? const SizedBox.shrink()
      : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Padding(
              padding: const EdgeInsets.only(top: 16, bottom: 4),
              child: Text(title,
                  style: const TextStyle(fontWeight: FontWeight.bold))),
          ...children,
        ]);

  @override
  Widget build(BuildContext context) {
    final c = _case;
    return Scaffold(
      appBar: AppBar(title: Text(widget.title, maxLines: 1)),
      floatingActionButton: FloatingActionButton.extended(
          onPressed: () => _openSource(),
          icon: const Icon(Icons.play_circle),
          label: const Text('Источник')),
      body: c == null
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(12), children: [
              Text('${c['vehicle']?['make'] ?? ''} ${c['vehicle']?['model'] ?? ''} '
                  '${c['vehicle']?['engine'] ?? ''} | ${c['system'] ?? ''}'),
              if ((c['applicability'] ?? 'model') != 'model')
                Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Chip(
                        avatar: const Icon(Icons.public, size: 16),
                        label: Text(c['applicability_note'] ??
                            c['applicability'] ?? ''))),
              const SizedBox(height: 8),
              Text(c['problem_summary'] ?? '',
                  style: Theme.of(context).textTheme.titleMedium),
              _section('💡 Причина', [Text(c['root_cause'] ?? '')]),
              _section(
                  '🔊 Звуки',
                  [
                    for (final s in (c['sounds'] as List? ?? []))
                      ListTile(
                          dense: true,
                          title: Text(s['description'] ?? ''),
                          subtitle: Text([
                            s['when'] ?? '',
                            (s['depends_on'] ?? '').isEmpty
                                ? ''
                                : 'меняется: ${s['depends_on']}',
                            (s['suspected_source'] ?? '').isEmpty
                                ? ''
                                : '→ ${s['suspected_source']}',
                          ].where((e) => e.isNotEmpty).join(' | ')),
                          trailing: (s['timestamp_sec'] ?? 0) > 0
                              ? IconButton(
                                  icon: const Icon(Icons.play_arrow),
                                  onPressed: () =>
                                      _openSource(s['timestamp_sec']))
                              : null),
                  ]),
              _section(
                  '🔍 Диагностика',
                  [
                    for (final s in (c['diagnostic_steps'] as List? ?? []))
                      ListTile(
                          dense: true,
                          leading: Text('${s['order']}'),
                          title: Text(s['action'] ?? ''),
                          subtitle: (s['detail'] ?? '').isEmpty ? null : Text(s['detail']),
                          trailing: (s['timestamp_sec'] ?? 0) > 0
                              ? IconButton(
                                  icon: const Icon(Icons.play_arrow),
                                  onPressed: () => _openSource(s['timestamp_sec']))
                              : null),
                  ]),
              _section(
                  '🔧 Ремонт',
                  [
                    for (final s in (c['repair_steps'] as List? ?? []))
                      ListTile(
                          dense: true,
                          leading: Text('${s['order']}'),
                          title: Text(s['action'] ?? '')),
                  ]),
              _section(
                  '📏 Замеры',
                  [
                    for (final m in (c['measurements'] as List? ?? []))
                      ListTile(
                          dense: true,
                          title: Text(m['what'] ?? ''),
                          subtitle: Text(
                              'норма: ${m['expected'] ?? '-'} | факт: ${m['actual'] ?? '-'} ${m['tool'] ?? ''}')),
                  ]),
              _section(
                  '⚠️ Нюансы',
                  [
                    for (final p in (c['pitfalls'] as List? ?? []))
                      ListTile(
                          dense: true,
                          leading: const Text('!'),
                          title: Text(p['text'] ?? ''),
                          trailing: (p['timestamp_sec'] ?? 0) > 0
                              ? IconButton(
                                  icon: const Icon(Icons.play_arrow),
                                  onPressed: () => _openSource(p['timestamp_sec']))
                              : null),
                  ]),
              const SizedBox(height: 72),
            ]),
    );
  }
}
