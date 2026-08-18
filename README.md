# kobito

Linear等のissueトラッカーを定期的に自律実行で消化し、判断が要る場面だけ人間に通知して介入を仰ぐワーカー。

「小人と靴屋」の童話（人が寝ている間に小人が勤勉に仕事をしておく）のモチーフで、不在中も自律的に作業が進む状態を目指す。

判断・確認が必要な場面は [human-agent-board](https://github.com/fezzlk/human-agent-board) 経由でユーザーへエスカレーションする。連携先はLinearに限定しない。

作業中はissue単位の状態（調査中・実装中・検証中・判断待ち・PR待ち等）をhuman-agent-boardへ上書きし、重要な状態遷移だけLINEへ通知する。ユーザーはLINE Botへ「kobito状況」と送ることで、進行中作業と直近の完了・失敗をいつでも確認できる。タスク・優先度・正式な完了状態の正本はissueトラッカー、human-agent-boardの状態はリアルタイム表示用のスナップショットとする。

## 実行手順

kobitoが1回の起動で行う具体的な手順（issueの探索・着手可否判断・claim・作業・エスカレーション）は [OPERATING.md](./OPERATING.md) を参照。

## 実行基盤

kobito自体は特定の実行基盤に依存しない設計だが、現状の運用では以下を想定している:

- **トリガー**: 定期実行の起点（cron等）が、実行環境へ「OPERATING.mdの手順に従え」という短い指示を送るだけの薄いトリガーとして動作する。
- **実行**: 実際の作業（issue調査・実装・PR作成・human-agent-boardとのやり取り）は、GitHub・issueトラッカー・human-agent-boardへの書き込み権限を持つ環境（例: 個人のリモート実行基盤）で行う。

個人運用における具体的な構成（どのトリガー・どの実行環境を使っているか）は https://github.com/fezzlk/pico の `projects/kobito.md` を参照。

設計の背景・決定の詳細も同ファイルを参照。
