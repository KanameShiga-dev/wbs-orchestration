# wbs-orchestration 修正案（H1・H2）2026-11-03

ドラフトの解析で見つかった H1（タイムアウト時に子プロセスが残る）と H2（権限バイパスの検査の抜け）に対する修正。
このリポジトリでは、1 つ目のコミットが元のドラフト、2 つ目のコミットがこの修正にあたる。

| 成果物 | 場所 |
|---|---|
| 修正したスキル一式 | `wbs-orchestration/` |
| 差分 | 2 つ目のコミット（`git show HEAD`） |
| 変更したファイル | `scripts/wbs.py`、`tests/test_wbs.py`、`SKILL.md`、`examples/config.example.json` |

**単体テストは利用者の環境で 27 件すべて成功した（9 章）。** 実際のエージェント CLI での試行はまだ。

---

## 1. 修正の全体像

| 指摘 | 修正 | 効果 |
|---|---|---|
| H1 子プロセスが残る | `ProcessTree` クラスを追加。Windows は Job Object、それ以外はプロセスグループで、CLI と CLI が起動した全プロセスを終了する | タイムアウト時、正常終了の後、`wbs.py` 自体が kill されたとき（Windows）のどれでも、裏で編集が続かない |
| H2-a 検査の抜け | 禁止語に `danger` と `approve-for-me` を追加。承認系のフラグに続く `never` を拒否。`argv[0]` がスクリプトのときは明示の確認を必須にした | `danger-full-access`、`-a never`、`--ask-for-approval=never`、ラッパースクリプトの素通りを止める |
| H2-b 承認待ちが伝わらない | 任意設定 `report_dir` を追加。CLI に報告ファイルを書かせ、1 行目の `STATUS:` だけを読む | 「承認が要るので止まった」が `failed` として即座に見える。報告の本文は DB に入れない |
| M1（ついで） | `.cmd` で入る CLI の指定方法を SKILL.md に書いた | npm 版の CLI でつまずかない |
| L4（ついで） | `mark_started` が失敗したときも `finally` で CLI を終了する | CLI が stdin 待ちのまま取り残されない |

---

## 2. H1: プロセスツリーの終了

### 原因
`Popen.kill()` は Windows では `TerminateProcess()` を呼ぶだけで、子孫のプロセスを終了するとは書かれていない（Python 公式ドキュメント）。エージェント CLI はランチャーから本体や作業用のプロセスを起動するので、タイムアウトしても子孫が編集を続けうる。

### 修正内容（`scripts/wbs.py`）
- **Windows**: CLI の起動直後に Job Object を作り、`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` を設定して CLI を割り当てる。
  - タイムアウト時: `TerminateJobObject` でジョブ内の全プロセスを終了する。
  - 正常終了の後: ジョブのハンドルを閉じ、残ったプロセスを終了する。
  - `wbs.py` 自体がタスクスケジューラーの実行時間の上限などで kill されたとき: OS がハンドルを閉じるので、CLI のツリーも終了する。
  - Job Object を作れなかったとき: タイムアウト時に `taskkill /T /F /PID` で代替する。
- **POSIX**: `start_new_session=True` で CLI を独立したセッションのリーダーとして起動し、`os.killpg(..., SIGKILL)` でグループごと終了する。
- 結果のメモに終了の方法（`ended by job object` など）を記録する。

### 公式ドキュメントで確認した値
| 項目 | 値 | 出典 |
|---|---|---|
| `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` | `0x00002000`。拡張構造体が必要 | Microsoft Learn: JOBOBJECT_BASIC_LIMIT_INFORMATION |
| `JobObjectExtendedLimitInformation` | `9` | Microsoft Learn: SetInformationJobObject |
| 基本構造体のフィールドの順序と型 | LARGE_INTEGER×2、DWORD、SIZE_T×2、DWORD、ULONG_PTR、DWORD×2 | 同上 |
| 拡張構造体 | Basic、IO_COUNTERS、SIZE_T×4 | Microsoft Learn: JOBOBJECT_EXTENDED_LIMIT_INFORMATION |
| `Popen.kill()`（Windows） | `terminate()` の別名で、`TerminateProcess()` を呼ぶ | docs.python.org: subprocess |
| `start_new_session` | POSIX のみ。子プロセスで `setsid()` を呼ぶ | 同上 |

`IO_COUNTERS` のメンバー（ULONGLONG×6）は Microsoft のページに表示がなかったので、**記憶から書いている**。構造体のサイズに関わるので、実機で `SetInformationJobObject` が成功するか確かめる必要がある（失敗した場合は `taskkill` に代わるだけで、壊れはしない）。

### 残るリスク
- CLI が Job Object に入る前の一瞬（`Popen` の直後から割り当てまで）に起動した子プロセスは対象外。ランチャーが子を起動するまでには通常それより時間がかかるが、保証はない。
- Popen の非公開属性 `_handle` を使っている（長年変わっていないが、公開 API ではない）。
- POSIX で `wbs.py` が SIGKILL されると、CLI のグループは残る（SKILL.md に記載した）。

---

## 3. H2-a: バイパス検査の強化

### 公式ドキュメントで確認したフラグ
| CLI | フラグ | 修正前 | 修正後 |
|---|---|---|---|
| Codex | `--sandbox danger-full-access` | 通る | **拒否** |
| Codex | `--ask-for-approval never` / `-a never`（サンドボックスの指定なし、または狭くない指定） | 通る | **拒否** |
| Codex | `--sandbox workspace-write`（か `read-only`）と `-a never` の組み合わせ | 通る | **通す**（案 A に合う。4 章） |
| Codex | `--approve-for-me`（別名 `--not-so-yolo`） | 別名だけ拒否 | **両方拒否** |
| Codex | `--dangerously-bypass-approvals-and-sandbox` / `--yolo` | 拒否 | 拒否 |
| Claude Code | `--dangerously-skip-permissions` | 拒否 | 拒否 |
| Claude Code | `--permission-mode bypassPermissions` | 拒否 | 拒否 |
| Claude Code | `--permission-mode auto`（`=auto` も） | 通る | **拒否**（案 C に当たる） |
| Claude Code | `--permission-mode acceptEdits / dontAsk` | 通る | **通す**（案 A に合う） |

出典: learn.chatgpt.com の Codex CLI コマンドリファレンス（developers.openai.com からのリダイレクト先）、code.claude.com の CLI reference。

**訂正**: 解析の段階で挙げた Codex の `--full-auto` は、現在の公式リファレンスに載っていなかった。修正案では対象にしていない。

### 修正内容
- `BYPASS_MARKERS` に `danger`（`dangerous` を含む）と `approve-for-me` を加えた。
- `--permission-mode` の値が `auto` か `bypassPermissions` なら拒否する。
- `-a`、または名前に `approval` を含むフラグの値が `never` なら拒否する（`=` で書いた形と、次の引数に書いた形の両方）。ただし、`-s` / `--sandbox` を明示し、その値がすべて `read-only` か `workspace-write` なら通す。
- `argv[0]` が `.cmd / .bat / .ps1 / .sh / .vbs` のときは、その CLI の設定に `"wrapper_reviewed": true` がない限り拒否する。スクリプトの中で追加されるフラグは検査できないため。

---

## 4. 決定: 無人の CLI に与える許可は案 A（2026-11-03）

**利用者が案 A を選んだ。** SKILL.md に「Permission policy」の節を追加し、`wbs.py` の検査を案 A に合わせた。

### 決定に合わせて変えたこと
- **`-a never` の扱いを見直した**。Codex の公式ドキュメントによると、`workspace-write` は既定でネットワークを切り、作業フォルダと一時フォルダだけに書き込める（`.git` は読み取り専用）。この中で承認を切ると、サンドボックスの外に出る操作は、承認を待たずに失敗する。これは案 A そのものなので、狭いサンドボックスを明示した場合だけ `never` を通すようにした。
- **Claude Code の `auto` を拒否するようにした**。公式の説明は「すべてを背景の安全チェックつきで実行する」で、案 C に当たる。`dontAsk`（承認が必要な呼び出しをすべて拒否し、入力を待たない）と `acceptEdits`（作業フォルダ内の編集と、よく使うファイル操作のコマンドだけを承認する）は通す。
- SKILL.md に、案 A に合う CLI ごとの設定例を載せた。サンドボックスの中の CLI はコミットできないことがあるので、受け入れ条件はファイルとテストで書き、コミットは人が行う、という注意も加えた。
- `report_dir` を git の管理対象から外すよう SKILL.md に書いた。

### 試行で確かめる項目（公式ドキュメントで確認できなかったもの）
- `codex exec` が `--ask-for-approval` を受け付けるか。リファレンスの exec の節には載っていない。受け付けない場合は、利用者の Codex の設定ファイルで承認の方針を指定する。
- `claude -p` が、プロンプトの引数を付けないときに stdin からプロンプトを読むか。

### 検討した案（記録として残す）

**選んだ案: A（プロジェクト内の書き込みだけを許し、それ以外は報告して止める）。**
理由: 実装タスクを無人で進められる最小の範囲で、外への影響（ネットワーク、プロジェクト外のファイル、git push）は承認の側に残る。止まった場合は H2-b の報告ファイルで即座に見える。

| 案 | 内容（例） | 無人でできること | 意図しない変更の危険 | 止まる頻度 |
|---|---|---|---|---|
| **A（推奨）** | Codex `--sandbox workspace-write`、Claude `--permission-mode acceptEdits` と `--allowedTools` で許すコマンドを限定 | プロジェクト内の編集と、許したコマンド | 低（プロジェクト内に限られる） | 中 |
| B | 読み取りだけ（Codex `--sandbox read-only`、Claude `--permission-mode plan` など）。実装は対話セッションで行う | 設計・レビュー・調査 | 最小 | 低（何もしないため） |
| C | Claude `--permission-mode auto`（分類器が判断する） | A より広い | 中（分類器の判断に依存する） | 低 |
| D | 隔離した VM・コンテナでの全面バイパス | すべて | 隔離の質しだい。このスキルの範囲外 | なし |


---

## 5. H2-b: 報告ファイルによる「承認待ち」の通知

### 修正内容
- 設定に任意の `report_dir` を追加した。相対パスは `run` の作業フォルダ（プロジェクト）を基準にする。サンドボックスが書き込める場所に置くため。
- `report_dir` があると、プロンプトに次を加える。
  - `Report file: <report_dir>/<task>-attempt<N>.md`
  - 1 行目は `STATUS: done` / `STATUS: needs-approval` / `STATUS: blocked` のどれか
  - 承認が必要な操作は行わず、`needs-approval` を書いて終了すること
- 終了コード 0 のとき、**1 行目だけ**を読む（最大 200 文字）。

| 1 行目 | タスクの状態 | 意味 |
|---|---|---|
| `STATUS: done` | `awaiting_review` | 通常どおりレビューへ |
| `STATUS: needs-approval` / `blocked` | `failed`（全体が止まる） | 人がすぐ見る |
| ファイルがない | `awaiting_review`、`report: missing` | レビュアーに「報告がない」と分かる |
| 形式が違う | `awaiting_review`、`report: unrecognized` | 同上 |

- 報告の本文はプロジェクトに残り、DB には状態の語だけが入る（秘密情報を DB に入れない方針を保つ）。
- `run` の出力に `report` と `report_file` を加えた。
- `report_dir` を書かない既存の設定は、これまでと同じに動く（後方互換）。

### 限界
報告は CLI 自身の申告で、`done` と書いても作業が正しいとは限らない。判断はこれまでどおりレビュアーが行う。

---

## 6. 追加したテスト（`tests/test_wbs.py`）

| テスト | 確かめること |
|---|---|
| `test_permissions_beyond_policy_a_and_unreviewed_wrapper_are_refused` | `danger-full-access`、サンドボックスなしの `-a never`、`--approve-for-me`、`bypassPermissions`、`auto` を拒否する。`workspace-write` か `read-only` と `never` の組み合わせ、`acceptEdits`、`dontAsk` は通す。`.cmd` は `wrapper_reviewed` がなければ拒否する |
| `test_timeout_ends_every_process_the_cli_started` | ダミー CLI が起動した孫プロセス（ハートビートを書き続ける）が、タイムアウトの後に止まる |
| `test_processes_left_after_a_normal_exit_are_ended` | 正常終了の後に残った孫プロセスも止まる |
| `test_report_status_decides_between_review_and_stop` | `done` はレビューへ、`needs-approval` は終了コード 0 でも `failed`。本文は DB に入らない |
| `test_missing_report_is_flagged_but_still_goes_to_review` | 報告がなければ `missing` としてレビューへ。プロンプトに `Report file:` が入る |

既存のテストは変えていない（ダミー CLI のモードと設定の補助関数を拡張しただけ）。

---

## 7. 適用の手順

`wbs-orchestration/` を、使うエージェントのスキルのフォルダに置く。テストは `wbs-orchestration/` の中で実行する。

```bash
python -m unittest discover -s tests -v
```

続けて、架空の計画で実際の CLI を 1 本、`timeout_seconds` を短くして試す。タイムアウトの後にタスクマネージャーで CLI のプロセスが残っていないことを確認する。

## 8. 元に戻す手順

修正前のドラフトは 1 つ目のコミットに残っている。`git revert HEAD` で修正だけを取り消せる。

## 9. 検証の記録

- 実行した: 編集した `wbs.py` と `test_wbs.py` の構文解析（`ast.parse`）、`config.example.json` の JSON 解析 → いずれも成功。差分にユーザー名を含むパスがないことを確認した。
- 利用者が実行した（2026-11-03、Windows・PowerShell 5.1）: `python -m unittest discover -s tests -v` → 27 件すべて成功（既存 22 件、追加 5 件）。正常終了の後に孫プロセスが止まるテストの成功は、Job Object の設定（`SetInformationJobObject` を含む）がこの環境で成功したことを示す。
- 実行していない: 実際の CLI での `run`、POSIX 環境でのテスト。
- 公式ドキュメントで確認した: Job Object の定数と構造体、Popen の `kill` と `start_new_session`、CreateProcess の `.exe` 補完とバッチファイルの扱い、Codex と Claude Code の権限関連のフラグ、Codex の `workspace-write` の範囲（ネットワークが既定でオフ、書き込めるのは作業フォルダと一時フォルダ、`.git` は読み取り専用）、Claude Code の各権限モードの意味。
- 記憶から書いている: `IO_COUNTERS` のメンバー構成。
