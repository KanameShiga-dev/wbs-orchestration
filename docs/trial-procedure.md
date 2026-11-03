# wbs-orchestration 試行手順（実際の CLI）2026-11-03

このリポジトリの `wbs-orchestration/` を、本物のエージェント CLI で試すための手順。
単体テスト（27 件成功）では確かめられない、次の点を確認する。

| 番号 | 確かめること | シナリオ |
|---|---|---|
| Q1 | CLI が stdin のプロンプトを読み、案 A の権限で作業し、報告ファイルを書けるか | S1 |
| Q2 | レビューを経てから依存タスクが進むか（`accept` / `reject` / `retry`） | S1 |
| Q3 | 承認が要る操作（ネットワーク）で CLI が止まり、`needs-approval` として見えるか | S2 |
| Q4 | タイムアウト時に、CLI が起動したプロセスがすべて終わるか（H1） | S3 |
| Q5 | `wbs.py` が途中で止められたとき、CLI も終わり、試行は開いたまま残るか | S4 |
| Q6 | 未確認の 2 点: `codex exec` が `--ask-for-approval` を受け付けるか、`claude -p` が stdin からプロンプトを読むか | S0 |

## 0. 前提と安全のための約束

- 試行は、新しく作る架空のプロジェクトだけで行う。実在のリポジトリの中では行わない。
- プランと設定に、秘密情報・個人情報・実在の固有名を書かない。
- **CLI を起動するたびに利用枠（クォータや料金）を使う。** 全シナリオで CLI の起動はおよそ 8〜10 回。
- タスクスケジューラーには登録しない（最後に `-WhatIf` で表示だけ確かめる。任意）。
- コマンドは Windows PowerShell 5.1 の書き方。プロンプトは英語で書く（PowerShell 5.1 からネイティブコマンドへのパイプは既定で ASCII になり、日本語が化けるため。`wbs.py` 自身は UTF-8 で渡すので、プランの中身はこの制約を受けない）。

## 1. 準備

### 1-1. 変数とフォルダ

以降の手順は、同じ PowerShell ウィンドウで続けて実行する。

```powershell
$Skill = "<このリポジトリの場所>\wbs-orchestration"
$Trial = "<試行用の新しいフォルダ>"   # 例: C:\path\to\wbs-trial
New-Item -ItemType Directory "$Trial\project", "$Trial\state", "$Trial\snapshots" | Out-Null
```

### 1-2. 架空のプロジェクト

git で管理し、CLI が何を変えたかを `git diff` で見られるようにする。

```powershell
Set-Location "$Trial\project"
git init
Set-Content .gitignore ".wbs-reports/`n__pycache__/"
Set-Content README.md "Fictional textstats package for a WBS trial."
git add . ; git commit -m "Initial fictional project"
```

### 1-3. CLI の場所を調べる

使う CLI だけを調べる（片方だけでよい）。

```powershell
Get-Command codex, claude -ErrorAction SilentlyContinue | Format-List Name, Source
```

- `Source` の**フルパス**を設定の `argv[0]` に使う。
- 拡張子が `.cmd` / `.ps1` のときは、そのファイルを開いて中身（別の実行ファイルを呼ぶだけか）を読み、問題なければ設定に `"wrapper_reviewed": true` を付ける。`.ps1` は `wbs.py` から直接は起動できないので、同じフォルダの `.cmd` か `.exe` を使う。

## 2. S0: 事前確認（Q6）

CLI を `wbs.py` なしで 1 回ずつ呼び、2 つの未確認点を確かめる。読み取り専用で、ファイルは変わらない。

**Codex**

```powershell
codex exec --help | Select-String "approval|sandbox"
```

```powershell
"Reply with the single word OK." | codex exec --sandbox read-only --ask-for-approval never -
```

- `--help` に `--ask-for-approval` が出ない、または 2 つ目がオプションのエラーになる場合は、設定の argv から `--ask-for-approval never` を外す。そのうえで、Codex の設定ファイルの承認の方針を利用者が確認する。
- `codex exec` は git リポジトリの外ではエラーになることがある（記憶から書いている）。1-2 で git にしてあるので、エラーになったら作業フォルダが `$Trial\project` か確かめる。

**Claude Code**

```powershell
"Reply with the single word OK." | claude -p --permission-mode dontAsk
```

- OK が返れば、引数なしの `-p` が stdin のプロンプトを読むことの確認になる。

| 確認 | 結果（記入） |
|---|---|
| `codex exec` が `--ask-for-approval` を受け付ける | はい / いいえ |
| `claude -p` が stdin のプロンプトを読む | はい / いいえ |

## 3. 設定ファイル

`$Trial\state\config.json` を作る。`<...>` は 1-3 で調べたフルパスに置き換える（JSON の中の `\` は `\\` と書く）。片方の CLI しか使わないなら、`roles` の両方をその CLI に向ける。

```json
{
  "warning_dir": "warnings",
  "report_dir": ".wbs-reports",
  "clis": {
    "claude": {
      "argv": ["<claude のフルパス>", "-p", "--permission-mode", "dontAsk",
               "--allowedTools", "Read", "Edit", "Write", "Bash(python -m unittest *)"],
      "timeout_seconds": 900
    },
    "codex": {
      "argv": ["<codex のフルパス>", "exec", "--sandbox", "workspace-write", "--ask-for-approval", "never", "-"],
      "timeout_seconds": 900
    }
  },
  "roles": {
    "implementer": "claude",
    "reviewer": "codex"
  }
}
```

- `report_dir` は `run` の作業フォルダ（`$Trial\project`）から見た場所になる。`warning_dir` は設定ファイルのフォルダから見た場所になる。
- `.cmd` を使う CLI には `"wrapper_reviewed": true` を足す。
- 設定だけを CLI を起動せずに確かめるコマンドはない（`run` は準備のできたタスクがあると CLI を起動する）。S1 の初回の `run` で設定エラー（終了コード 1）が出ないことで確かめる。

## 4. S1: 通常の流れ（Q1・Q2）

### プラン

`$Trial\state\plan-s1.json`:

```json
{
  "plan": {"name": "Fictional textstats trial", "goal": "Add a word counter to a fictional package."},
  "tasks": [
    {"id": "T1", "title": "Implement word_count", "role": "implementer",
     "description": "In the project root, create textstats.py with word_count(text) that returns the number of whitespace-separated words, and test_textstats.py with unittest tests. Do not commit.",
     "acceptance": "python -m unittest passes. Tests cover empty text, one word and several words with mixed whitespace."},
    {"id": "T2", "title": "Review word_count", "role": "reviewer", "depends_on": ["T1"],
     "description": "Review textstats.py and test_textstats.py in the project root. Write findings to REVIEW.md. Do not change other files and do not commit.",
     "acceptance": "REVIEW.md lists findings with severity, or states that none were found."},
    {"id": "T3", "title": "Decide whether to keep the change", "role": "owner", "depends_on": ["T2"],
     "requires_approval": true,
     "description": "A person reads REVIEW.md and the diff, then decides.",
     "acceptance": "The decision is recorded with complete or fail."}
  ]
}
```

### 手順

```powershell
Set-Location "$Trial\project"
$Db = "$Trial\state\s1.sqlite3"; $Cfg = "$Trial\state\config.json"
python "$Skill\scripts\wbs.py" --db $Db init --plan "$Trial\state\plan-s1.json"
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli claude ; "exit=$LASTEXITCODE"
```

確認すること:
1. 出力が `"result": "awaiting_review"`、`"report": "done"` になる。
2. `git status` と `git diff` で、`textstats.py` と `test_textstats.py` だけが増えている。
3. `.wbs-reports\T1-attempt1.md` の 1 行目が `STATUS: done` で、変更内容と検証方法が書かれている。
4. 自分でも `python -m unittest` を実行して通る。

レビューして受け入れる（この試行ではあなたがレビュアー）:

```powershell
python "$Skill\scripts\wbs.py" --db $Db accept --task T1 --reviewer owner --note "Trial check"
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli codex ; "exit=$LASTEXITCODE"
```

5. T2 が `awaiting_review` になり、`REVIEW.md` ができている。`textstats.py` は変わっていない（`git diff`）。

**差し戻しも 1 回試す**:

```powershell
python "$Skill\scripts\wbs.py" --db $Db reject --task T2 --reviewer owner --reason "Trial rejection"
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli codex ; "exit=$LASTEXITCODE"
```

6. `blocked`（終了コード 3）で、CLI は起動しない（`"cli_invoked": false`）。

```powershell
python "$Skill\scripts\wbs.py" --db $Db retry --task T2
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli codex
python "$Skill\scripts\wbs.py" --db $Db accept --task T2 --reviewer owner
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli claude
```

7. retry の後は `T2-attempt3.md` のように新しい報告ができる。最後の `run` は `idle` で、理由に `need user approval` が出る（T3 は人の担当）。

```powershell
python "$Skill\scripts\wbs.py" --db $Db claim --worker owner --task T3
python "$Skill\scripts\wbs.py" --db $Db complete --attempt <表示された attempt> --note "Trial decision"
python "$Skill\scripts\render_wbs.py" --db $Db --output "$Trial\snapshots\s1.html"
```

8. `s1.html` で全タスクが `done` になっている。

## 5. S2: 承認が要る操作で止まる（Q3）

ネットワークが必要なタスクを渡す。案 A ではネットワークが許されていないので、CLI が止まって `needs-approval` を報告するのが期待どおり。`example.com` は例示用に予約されたドメイン。

`$Trial\state\plan-s2.json`:

```json
{
  "plan": {"name": "Fictional network trial"},
  "tasks": [
    {"id": "N1", "title": "Save a web page", "role": "implementer",
     "description": "Download https://example.com/ and save it as page.html in the project root.",
     "acceptance": "page.html exists."}
  ]
}
```

```powershell
$Db = "$Trial\state\s2.sqlite3"
python "$Skill\scripts\wbs.py" --db $Db init --plan "$Trial\state\plan-s2.json"
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli claude ; "exit=$LASTEXITCODE"
```

| 結果 | 判定 |
|---|---|
| `failed`、`"report": "needs-approval"` か `"blocked"`、終了コード 3 | **期待どおり** |
| `awaiting_review`、`page.html` がない | 部分的。CLI が報告ファイルの指示に従わなかった。`report` の値（missing / done）を記録する |
| `page.html` ができた | **不合格**。案 A の外の操作ができてしまった。設定（許可したツール、CLI の設定ファイル）を見直す |

Codex でも同じことを試す場合は、`roles` の `implementer` を `codex` に変えて、新しい DB で行う。

## 6. S3: タイムアウトでプロセスが残らない（Q4）

設定の `timeout_seconds` を一時的に **20** にした別の設定ファイル `config-timeout.json` を作る（本体の設定は変えない）。エージェントの起動と考える時間で 20 秒を超えるので、タイムアウトになる。

```powershell
$Db = "$Trial\state\s3.sqlite3"
python "$Skill\scripts\wbs.py" --db $Db init --plan "$Trial\state\plan-s1.json"
$Before = Get-Process | Select-Object Id, Name
python "$Skill\scripts\wbs.py" --db $Db run --config "$Trial\state\config-timeout.json" --cli claude ; "exit=$LASTEXITCODE"
Start-Sleep -Seconds 3
$After = Get-Process | Select-Object Id, Name
Compare-Object $Before $After -Property Id, Name | Where-Object SideIndicator -eq "=>"
```

確認すること:
1. 出力の `exit_code` が `null`、終了コードが 3。
2. 最後のコマンドで、CLI に関係するプロセス（`node`、`claude`、`codex`、`cmd` など）が**残っていない**。同じ時間に自分で起動した無関係のプロセスが出ることはある。
3. DB の試行のメモに `ended by job object` と記録されている。

```powershell
python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select id, note from attempts').fetchall())" $Db
```

`ended by taskkill /T` と出た場合は、Job Object を作れなかった。理由を調べる必要がある（記録して報告）。

## 7. S4: 途中で止めたとき（Q5）

`wbs.py` を CLI の実行中に強制終了する。タスクスケジューラーの実行時間の上限で止められる場合と同じ状況。

ウィンドウ A（いつもの PowerShell）:

```powershell
$Db = "$Trial\state\s4.sqlite3"
python "$Skill\scripts\wbs.py" --db $Db init --plan "$Trial\state\plan-s1.json"
python "$Skill\scripts\wbs.py" --db $Db run --config $Cfg --cli claude
```

ウィンドウ B（別の PowerShell）で、A の `run` が始まって 10 秒ほどしてから:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like "*wbs.py*run*" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

確認すること:
1. 数秒後、CLI に関係するプロセスが残っていない（S3 と同じ方法で確かめる）。
2. A に戻って同じ `run` をもう一度実行すると、`blocked` になり、理由に `open CLI attempt(s) [(1, 'T1')]` が出る。CLI は起動しない。
3. 復旧の手順が通る:

```powershell
python "$Skill\scripts\wbs.py" --db $Db fail --attempt 1 --reason "Trial interruption"
python "$Skill\scripts\wbs.py" --db $Db retry --task T1
```

4. `git status` で、中断された CLI が途中まで書いたファイルがあれば確認して、必要なら捨てる（`git clean -n` で表示してから決める）。

## 8. 任意: スケジュール登録の表示だけ

```powershell
& "$Skill\scripts\register_schedule.ps1" -TaskName "WBS trial run" -PythonExe (Get-Command python).Source `
  -DbPath "$Trial\state\s1.sqlite3" -ConfigPath $Cfg -CliName claude `
  -WorkingDirectory "$Trial\project" -IntervalMinutes 30 -TimeLimitMinutes 60 -WhatIf
```

- `-WhatIf` なので登録はされない。実行ポリシーで止められたら、ここは飛ばす（ポリシーは変えない）。

## 9. 記録表

| シナリオ | 期待 | 結果（記入） | メモ |
|---|---|---|---|
| S0 Codex の承認フラグ | 受け付ける / 受け付けない | | |
| S0 Claude の stdin | 読む | | |
| S1-1〜4 実装タスク | awaiting_review、report done、テスト通過 | | |
| S1-5 レビュータスク | REVIEW.md のみ追加 | | |
| S1-6 差し戻し | blocked、CLI 起動なし | | |
| S1-7〜8 再実行と承認 | 新しい報告、最後は全 done | | |
| S2 ネットワーク | failed + needs-approval | | |
| S3 タイムアウト | プロセスなし、job object | | |
| S4 中断 | プロセスなし、blocked、復旧できる | | |

結果を貼ってもらえれば、合否の判定と、修正が要る点の整理をする。

## 10. 後片付けと元に戻す手順

- 試行で作ったものは `$Trial` の下だけ。削除すれば元に戻る。
- スケジュールは登録していない（8 は `-WhatIf` のみ）。
- 修正版のスキル本体は試行で変わらない（DB・設定・報告はすべて `$Trial` の下に作る）。
