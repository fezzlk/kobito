# 前提完了後の未完了Issue通知（簡易版）

前提Issueが未完了からcompletedになったとき、登録されたblocked-by関係から未完了の関連Issueを一覧で知らせる。正常な後続実装も含まれるため、取りこぼし・吸収漏れとの断定、コード差分による判定、自動クローズ、自動着手は行わない。設計Issueの完了と実装完了を混同しない。

## 取得・実行

通常の候補探索より前に、既存のLinear MCPを使ってスナップショットを作る。別のScheduler・クラウドサービス・APIキーは追加しない。

1. 対象ワークスペースのIssueを全ページ取得する。未着手候補だけに絞らず、進行中・完了・キャンセル・アーカイブ済みも含める。
2. 各IssueのblockedByを関係情報付きで取得する。ラベル、statusType、URL、タイトルも取得する。参照先のIssueも必ず取得し、同一Issueに同一のID体系を使う（表示識別子またはUUIDを混在させない）。parentIdやrelatedToから依存関係を推測しない。
3. 下記JSONに正規化する。observed_atは収集を開始したUTC時刻。全ページ・全関係を取得できたときだけcomplete=trueとする。途中失敗、権限不足、取得上限の場合は実行をスキップし、空の一覧や部分データで基準を上書きしない。
4. JSONをstdinでスクリプトへ渡す。シェル文字列へIssue本文を展開しない。既存の出力ファイル規則に従う一時JSONを使う場合は`--snapshot <path>`を指定できる。

```json
{
  "complete": true,
  "observed_at": "2026-09-17T00:00:00Z",
  "issues": [
    {"id": "P-1", "title": "設計", "url": "https://example.com/P-1", "status_type": "started", "blocked_by": [], "labels": []},
    {"id": "C-1", "title": "実装", "url": "https://example.com/C-1", "status_type": "backlog", "blocked_by": ["P-1"], "labels": []}
  ]
}
```

```bash
python3 ~/repos/kobito/scripts/absorption_guard.py \
  --state ~/repos/human-agent-board/board/state/absorption-guard.json \
  --snapshot <complete-snapshot.json> --dry-run
```

確認後、同じスナップショットで`--dry-run`を外す。dry-runは状態・通知を一切変更しない。初回の実行は比較用の基準を保存するだけで、既に完了している過去Issueの通知を大量発行しない。

## 通知と履歴

- completedへの遷移のみ対象。canceledになった前提は通知しない。関連Issueがcompleted/canceledなら除外する。
- 完了時に依存関係が削除されても、前回スナップショットにあった関係を照合する。それ以前に削除された関係は対象外。
- どちらかにkobito:ngがあれば静かに除外する。ラベル解除後の過去遷移の再通知は行わない。
- 前提ごとに1件のfyiへまとめる。リンクは本文に記載する（古いBoardで承認ボタンが付かないようrelated-linkは使わない）。LINE送信は既存Boardの通知設定に従う。
- 同じ前提・関連Issueの組み合わせは、Board通知を処理済みにしても、Issueを再オープンしても再通知しない。判断は関連Issueに記録し、実装が必要なら通常の着手承認手順へ進む。fyiへの反応を実装承認に流用しない。
- 状態ファイルには最新比較用スナップショット、未送信ペア、通知済みペアを保存する。複数worktree/同一ホストの実行は同じパスとロックを共有する。MacとVM間の状態共有は実装していないため、通知を担当するホストを一つに固定し、他ホストでは実行しない。
- Board登録失敗は未送信として再試行する。登録直後のクラッシュにも同じdedupe-keyを使う。ただし登録直後・履歴保存前にクラッシュし、再試行前にユーザーが通知を削除した場合の厳密なexactly-onceは保証しない。LINE送信自体の成功保証・再送はBoard側の責務。
- 状態ファイルを削除すると通知履歴が失われる。リセットを日常運用に組み込まない。Issueや状態の欠落・不明をDoneとして補完しない。

## 検証

```bash
python3 -m unittest discover -s tests -v
```

登録のない依存関係や、完了前の状態を観測できなかったIssueは検知できない。この簡易版はコード上の解決を保証する検査ではない。
