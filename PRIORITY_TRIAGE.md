# PRIORITY TRIAGE

kobitoが週1回、全プロジェクトを横断して優先度未設定のissueを集め、LINEからタップ1つで優先度を設定できるようにする運用手順。通常のissue実装を行う`OPERATING.md`（優先度順での候補選定、[FEZ-118](https://linear.app/fezzlk/issue/FEZ-118)）とは独立したパスとして実行する。

## 背景

`OPERATING.md`の候補issue選定はLinearの優先度（Urgent/High/Medium/Low/None）順に候補を見る。しかし優先度自体は作成者がその場で決めるだけで体系的な仕組みがなく、放置されると`None`のままになる。この手順は、優先度が付いていないissueをユーザーが週次でまとめて処理できるようにする。

## 目的

- 優先度未設定のまま放置されるissueを減らし、`OPERATING.md`の優先度順選定が実際に機能する前提を保つ
- ユーザーがLinearアプリで1件ずつ編集する手間を無くし、LINEのボタンタップだけで完結させる

## 安全境界

- 優先度の**変更提案は行わない**。既存issueの内容を評価・判断せず、優先度が未設定という事実だけを機械的に集める。
- issueの実装、コメント追加、ラベル変更、状態変更は行わない。優先度以外のフィールドには一切触れない。
- 認証情報の新規発行・変更、マージ・デプロイは行わない。

## 1回の実行手順

### 1. 優先度未設定issueを収集する

Linear MCPで全プロジェクトを横断し、以下を満たすissueを列挙する。

- 優先度が`None`（未設定）
- 状態が未完了（Backlog/Todo/In Progress等。Done/Canceled/Duplicateは除外）

該当issueが無ければ、この回はここで終了する（通知しない）。

### 2. 送信対象を選ぶ

作成日時が古い順に最大12件を選ぶ（LINE Flexカルーセルの表示上限に合わせる）。13件以上ある場合、今回送らなかった残りは翌週以降も同じ「古い順」ロジックで拾われるため、追加の状態管理は不要。

各issueについて`id`（LinearのUUID）・`identifier`（例: `FEZ-123`）・`title`・`project`・`url`を含むJSON配列を組み立てる。

### 3. human-agent-board経由でLINEへ送る

```bash
echo '<手順2で組み立てたJSON配列>' | python3 ~/repos/human-agent-board/board.py priority-digest
```

issueごとに1枚のLINEカード（Flexバブル）が届き、「Urgent/High/Medium/Low」ボタンをタップするとその場でLinearの優先度が更新される（`ai-gateway`の`/line/webhook`が`setpriority`postbackを処理し、Linear GraphQL APIへ書き込む。詳細: `human-agent-board`リポジトリの`ai-gateway`側実装`src/routes/line_webhook.py`）。

### 4. 記録する

送信件数・対象issue数をhuman-agent-boardのstatusスナップショットへ残す（通知はしない、`OPERATING.md`の`--notify`運用を踏襲）。

```bash
python ~/repos/human-agent-board/board.py status set \
  --source kobito --work-id priority-triage --state completed \
  --title "優先度未設定issueの週次ダイジェスト" \
  --summary "<対象件数、送信件数>" \
  --next-action "次回は残りの未設定issueを古い順に送信"
```
