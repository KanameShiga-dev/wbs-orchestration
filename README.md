# wbs-orchestration

変更を依存関係つきの WBS に分け、状態を SQLite に保存し、1 回の実行で準備のできたタスクを 1 件だけ、利用者が設定したエージェント CLI に渡すスキル。
CLI が正常に終わってもタスクは完了にならず、割り当てたレビュアー（人、または Codex などのエージェント）が `accept` / `reject` するまで止まる。

**状態: ドラフト。** 単体テストは Windows で 27 件すべて成功。実際のエージェント CLI での試行はまだ。

## 構成

| パス | 内容 |
|---|---|
| `wbs-orchestration/SKILL.md` | スキル本体（手順、状態遷移、コマンド、権限の方針） |
| `wbs-orchestration/scripts/wbs.py` | 状態管理と CLI への受け渡し（Python 標準ライブラリのみ） |
| `wbs-orchestration/scripts/render_wbs.py` | 状態を静的な HTML にする（DB は読み取り専用） |
| `wbs-orchestration/scripts/register_schedule.ps1` | Windows タスクスケジューラーへの登録（任意） |
| `wbs-orchestration/examples/` | 架空のプランと設定の例 |
| `wbs-orchestration/tests/` | 単体テスト |
| `docs/fix-proposal.md` | ドラフトに対する修正（H1・H2）と権限の方針（案 A）の記録 |
| `docs/trial-procedure.md` | 実際の CLI で試すための手順 |

## 使い方

`wbs-orchestration/` を、使うエージェントのスキルのフォルダに置く。詳しくは `wbs-orchestration/SKILL.md` を読む。

テスト（`wbs-orchestration/` の中で）:

```bash
python -m unittest discover -s tests -v
```

## 履歴

- 1 つ目のコミット: 受け取ったドラフト。
- 2 つ目のコミット: タイムアウト時に CLI の子プロセスまで終了する修正（H1）、権限バイパスの検査の強化と報告ファイル（H2）、無人の CLI に与える権限の方針（案 A）。

## 既知の制約

- 既存の別の WBS データベースとはスキーマに互換性がない。
- レビュー担当への自動の受け渡しは未実装。レビュアーが自分で `accept` / `reject` する。
- スケジュールは、利用者が `register_schedule.ps1` を実行したときだけ登録される。
