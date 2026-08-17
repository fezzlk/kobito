# OPERATING

kobitoが1回の実行（定期トリガーによる起動）で行う運用手順。トリガー方式や実行環境は問わない（ai-gateway経由でMac/VM上のClaude Codeが実行することを想定しているが、本手順自体は特定の実行基盤に依存しない）。

## 前提

- kobitoの起動指示を受けたClaude Codeセッションは、以下の手順を上から順に実行する。
- 判断に迷う場合、または実行環境の安全運用ルール（force-push・マージ・デプロイ・削除・一定額以上の費用が発生しうる操作など）に抵触する操作が必要になった場合は、必ず該当ステップで作業を止め、「エスカレーション」に従う。
- 本ドキュメントは汎用設計を前提とする。連携先のissueトラッカーやワークスペース固有の名称はここには書かない。

## 手順

### 1. ユーザーからの割り込み確認

[human-agent-board](https://github.com/fezzlk/human-agent-board) の `user-to-agent` キューを確認する。

```
python ~/repos/human-agent-board/board.py list --direction user-to-agent
```

保留中の依頼があれば内容に従う（例: 特定issueの優先、一時停止の指示など）。拾った依頼は着手時に`complete`で取り除く。

### 2. 候補issueの探索

連携しているissueトラッカー（現状はLinear）の全プロジェクトから、未着手の候補issueを探す。

- 既に`kobito:in-progress`ラベルが付いているissueは、他の実行が着手中とみなしスキップする。
- `kobito:plan-pending`ラベルが付いているissueは無条件スキップにはせず、「3-1. 計画レビュー待ちの確認」の対象として扱う。
- 対象範囲はプロジェクト・ラベルによる事前絞り込みをしない。着手可否の判断は次のステップで行う。

### 3. 着手可否の判断

#### 3-1. 計画レビュー待ちの確認（`kobito:plan-pending`ラベルが付いている場合）

human-agent-boardの`user-to-agent`キューに、このissueに対する承認（`related-link`がこのissueのURLと一致するもの）があるか確認する。

- 承認あり: ラベルを`kobito:in-progress`に戻し、対応する`user-to-agent`エントリを`complete`した上で「5. 作業」に進む（計画は既に`agent-to-user`へ書いた内容のまま進めてよく、あらためて4'を経由する必要はない）。
- 承認なし: このissueには今回は着手せず、次の候補に進む（再度plan_requestは書かない。催促は行わない）。

#### 3-2. 通常の着手可否判断

見つけた候補issueごとに、以下を確認する。いずれかに該当する場合はそのissueには着手せず、次の候補に進む（無理に着手しない）。

- issueの説明だけで作業を始めるのに十分な情報が揃っているか（要件が曖昧・複数解釈が可能な場合は着手しない）
- 作業の過程で以下のいずれかが必要になる見込みがないか:
  - 破壊的・不可逆な操作（削除、force-push、他者のブランチ/PRへの操作等）
  - マージ・デプロイ
  - 一定額（目安100円）以上の費用が発生しうる操作
  - 認証情報の新規発行・既存認証情報の変更
  - 上記以外で、実行環境の安全運用ルールに抵触する操作
- 対象リポジトリのCLAUDE.md等が、見込まれる変更規模（3ファイル以上にまたがる変更、既存の動作・構造への影響等）に対して対話的な計画承認（EnterPlanMode等）を必須としているか
  → 該当する場合は着手を中断し、「4'. 計画レビュー依頼」に進む（このissueにはこの場で実装しない）

着手できる候補が見つからなければ、その回の実行はここで終了する。

### 4. claim

着手するissueに`kobito:in-progress`ラベルを付与する。これにより、同時に複数の実行が同じissueに重複着手することを防ぐ。

### 4'. 計画レビュー依頼（対話的な計画承認が必須なリポジトリの場合）

実装せず、以下を行う。

1. issueに`kobito:plan-pending`ラベルを付与する（`kobito:in-progress`は付与しない）。
2. 計画内容（何を・どのファイルを・どう変更する予定か）をまとめ、human-agent-boardの`agent-to-user`へ書き込む。

   ```
   python ~/repos/human-agent-board/board.py add --direction agent-to-user \
     --from kobito --type plan_request \
     --title "<短い要約>" --body "<計画の内容>" \
     --related-link <issueのURL>
   ```

3. このissueへの着手はここで終了する。次の候補があれば「2. 候補issueの探索」に戻り、なければその回の実行を終了する。

承認された場合の再開は「3-1」を参照。

### 5. 作業

1. issueの内容を調査する。
2. 対象リポジトリで作業用ブランチを作成する。
3. 実装する。
4. pushし、PRを作成する（**マージはしない**）。
5. issueにPRへのリンクをコメントする。
6. `kobito:in-progress`ラベルを`kobito:pr-open`など作業完了を示すラベルに更新する。

作業の途中で「3. 着手可否の判断」に該当する操作が必要だと判明した場合は、その時点で作業を中断し、次の「エスカレーション」に進む。

### 6. エスカレーション

以下のいずれかに該当する場合、issueへの着手を中断し、human-agent-boardの`agent-to-user`へ書き込む。

```
python ~/repos/human-agent-board/board.py add --direction agent-to-user \
  --from kobito --type decision_request \
  --title "<短い要約>" --body "<状況・判断してほしい内容>" \
  --related-link <issueのURL>
```

- 着手可否の判断で迷った場合（要件が曖昧など）
- 作業中に破壊的・不可逆・費用発生・マージ/デプロイ等の操作が必要だと判明した場合
- その他、判断の権限がkobito自身にないと感じた場合

書き込んだら、そのissueに付けていた`kobito:in-progress`ラベルは外し、未着手の状態に戻す。

## 認可・安全性の前提

v1のkobitoが読み取る入力元は、issueトラッカー（ユーザーが管理するワークスペース）とhuman-agent-boardの`user-to-agent`キュー（ユーザー自身が書き込む場所）に限られる。外部からの未検証な入力（Webhook等、第三者が任意の内容を書き込める経路）は扱わない。「誰の指示で再開してよいか」の照合は、この入力元の限定によって担保する。

**例外（LINE Bot経由の承認/却下、2026-08-18〜）**: `human-agent-board`の`agent-to-user`書き込み時、`ai-gateway`の`/line/webhook`経由でLINEへ通知が送られ、ユーザーはLINE上のボタンから承認/却下できる（FEZ-91）。このルートは技術的にはWebhookだが、(1) LINEのX-Line-Signatureによる署名検証、(2) 送信元LINE userIdが固定の`LINE_AUTHORIZED_USER_ID`（ユーザー本人）と一致することの確認、の二重チェックにより、実質的に「ユーザー自身が書き込む」のと同じ信頼境界を保っている。第三者が任意の内容を注入できる経路ではないため、上記の原則には抵触しない。詳細: `human-agent-board`リポジトリの`ai-gateway`側実装（`src/routes/line_webhook.py`）。

## 対象外（v1のスコープ外）

- マージ・デプロイなど、PR作成より先の操作
- issueトラッカー・human-agent-board（LINE Bot経由の承認/却下を含む）以外からの指示の受け付け
