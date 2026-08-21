# OPERATING

kobitoが1回の実行（定期トリガーによる起動）で行う運用手順。トリガー方式や実行環境は問わない（ai-gateway経由でMac/VM上のClaude Codeが実行することを想定しているが、本手順自体は特定の実行基盤に依存しない）。

技術トレンドの週次調査はこの手順へ混在させず、[TECH_RADAR.md](./TECH_RADAR.md)を明示的に指定する別トリガーで実行する。サービス・事業アイデアの週次生成も[OPPORTUNITY_RADAR.md](./OPPORTUNITY_RADAR.md)を指定する独立トリガーで実行する。

## 前提

- kobitoの起動指示を受けたClaude Codeセッションは、以下の手順を上から順に実行する。
- 判断に迷う場合、または実行環境の安全運用ルール（force-push・マージ・デプロイ・削除・一定額以上の費用が発生しうる操作など）に抵触する操作が必要になった場合は、必ず該当ステップで作業を止め、「エスカレーション」に従う。
- 本ドキュメントは汎用設計を前提とする。連携先のissueトラッカーやワークスペース固有の名称はここには書かない。

## 作業状況の公開

kobitoは作業中、human-agent-boardのstatusスナップショットをissue単位で更新する。タスク・優先度・正式な完了状態の正本はissueトラッカー、statusはユーザーが「今どうなっているか」を確認するためのリアルタイム表示とする。

```bash
python ~/repos/human-agent-board/board.py status set \
  --source kobito --work-id <issue識別子> --state <状態> \
  --title "<issueタイトル>" --summary "<実施済み内容>" \
  --next-action "<次に行うこと>" \
  --related-link <issue URL> [--related-link <GitHub URL>] [--notify]
```

状態は以下を使う。

- `waiting`: 外部条件や実行枠を待っている
- `researching`: issue・コード・既存設計を調査中
- `implementing`: 実装中
- `verifying`: テスト・レビュー・動作確認中
- `decision_pending`: ユーザーの判断待ち
- `pr_open`: PR作成済み・レビュー待ち
- `completed`: kobitoとしての処理と後続確認が完了
- `failed`: 回復できない失敗で停止

`researching`・`implementing`・`verifying`などの細かな途中経過は`--notify`を付けず、スナップショットだけを更新する。`decision_pending`・`pr_open`・`completed`・`failed`など、ユーザーが知る意味のある遷移だけ`--notify`を付ける。これによりLINE通知過多を避ける。ユーザーはLINE Botへ「kobito状況」と送ることで、通知されない途中経過も確認できる。

現在状態は同じissueの更新で上書きする。`completed`・`failed`の履歴ファイルは自動削除せず保持し、通常の一覧とLINE返信では直近5件だけを表示する。

## 手順

### 1. ユーザーからの割り込み確認

[human-agent-board](https://github.com/fezzlk/human-agent-board) の `user-to-agent` キューを確認する。

```
python ~/repos/human-agent-board/board.py list --direction user-to-agent
```

保留中の依頼があれば内容に従う（例: 特定issueの優先、一時停止の指示など）。拾った依頼は着手時に`complete`で取り除く。

### 1-1. GitHub・Linear接続preflight

候補issueを探索する前に、agent-kitの共通preflightを対象リポジトリ候補ごとに実行する。対象がまだ決まっていない最初の検査ではkobito自身を使う。

```bash
python3 ~/repos/agent-kit/scripts/connectivity-preflight.py \
  --json --repo ~/repos/kobito --linear-write-allowed
```

`checks`の各能力は独立して扱う。特に`git_fetch`・`git_push`と`github_api`、`linear_read`・`linear_write`を同一視しない。

- `overall: OK`: 通常どおり「2. 候補issueの探索」へ進む。
- `overall: DEGRADED`: `recovery`と利用不能な能力を確認する。Linear writeを確認できない場合はissueのclaim・ラベル・status・コメントを更新できないため、**新規issueへは着手しない**。安全な読み取りと復旧調査だけを行える。
- `overall: BLOCKED`: 候補issueを探索・claimせず、その回の実行を停止する。

DEGRADEDまたはBLOCKEDで通常作業を開始できない場合、可能ならhuman-agent-boardへ以下のstatusを残す。加えて、ユーザー操作なしに復旧できない能力（認証設定、権限付与、接続設定等）が1つでもあれば、`agent-to-user`へ具体的な設定依頼を残す。`--dedupe-key connectivity-preflight`により同じ障害は新規通知を増やさず既存項目を更新する。通知自体が失敗しても再試行ループには入らず終了する。

```bash
python ~/repos/human-agent-board/board.py status set \
  --source kobito --work-id connectivity-preflight --state failed \
  --title "GitHub・Linear接続preflight" \
  --summary "<OK/DEGRADED/BLOCKEDと失敗した能力。秘密値は含めない>" \
  --next-action "<preflightが返した復旧手順>" --notify

python ~/repos/human-agent-board/board.py add \
  --direction agent-to-user --from kobito --type action_required \
  --dedupe-key connectivity-preflight \
  --title "kobitoの接続設定を修復してください" \
  --body "<失敗した能力、作業への影響、ユーザーが行う具体的な復旧手順、連続失敗回数>"
```

`action_required`は承認・却下を求める項目ではなく、ユーザーによる設定作業を求める通知として扱う。「確認してください」だけで終わらせず、実行するコマンドまたは設定画面、成功確認方法、kobitoが自動再試行する時刻を記載する。

通知にはトークン、APIキー、Authorizationヘッダー、コマンドへ注入された環境変数、認証URLを含めない。復旧後の次回起動でpreflightを再実行し、成功したら既存の障害項目を解消してから通常フローへ戻る。

```bash
python ~/repos/human-agent-board/board.py resolve \
  --direction agent-to-user --dedupe-key connectivity-preflight
```

復旧時はstatusを`completed`として`--notify`し、「接続が正常に戻り通常作業を再開する」と明示する。

### 2. 候補issueの探索

連携しているissueトラッカー（現状はLinear）の全プロジェクトから、未着手の候補issueを探す。

探索前に、既存のstatusスナップショットとissueトラッカーを突き合わせる。issueが既にDone/Canceledならstatusを`completed`に移し、残存する`kobito:in-progress`・`kobito:plan-pending`・`kobito:pr-open`ラベルを外す。これにより、古いラベルや状態を進行中として表示し続けない。

- 既に`kobito:in-progress`ラベルが付いているissueは、他の実行が着手中とみなしスキップする。
- `kobito:plan-pending`ラベルが付いているissueも無条件スキップにはせず、他の候補と同列に扱う（「3-1. 計画レビュー待ちの確認」の対象、詳細は後述）。
- 対象範囲はプロジェクト・ラベルによる事前絞り込みをしない。着手可否の判断は次のステップで行う。

集めた候補は、**全プロジェクト横断で優先度順（Urgent → High → Medium → Low → None）にソート**する。同一優先度内は作成日時が古い順（先に待っているものを優先）。プロジェクトごとの優先度づけはしない。`kobito:plan-pending`の候補も、承認済みだからといって優先度を無視して先頭に割り込ませたりはせず、同じ優先度順リストの中で扱う。

この優先度順に候補を1件ずつ見ていく。各候補について、まず**ブロック関係を確認する**（Linear issue取得時にブロック関係を含める）。自分をブロックしているissueが未完了（Done/Canceled以外）の場合、優先度に関わらずこの候補はスキップし、次の候補に進む。ブロックされていない候補が見つかったら、そこで初めて「3. 着手可否の判断」に進む。3-2で着手不可と判断された場合も、優先度順で次の候補に進む。

優先度順を最後まで見てもブロックされていない・着手可能な候補が無ければ、その回の実行はここで終了する。

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

claim直後、statusを`researching`として作成する。summaryには着手理由と最初に調べる対象、next-actionには直近の具体的な作業を記載し、issue URLを`related-link`に含める。この段階では`--notify`しない。

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

   合わせて同じissueのstatusを`decision_pending`へ更新し、`--notify`を付ける。summaryには判断が必要な理由、next-actionには承認後に最初に行う作業を記録する。

3. このissueへの着手はここで終了する。次の候補があれば「2. 候補issueの探索」に戻り、なければその回の実行を終了する。

承認された場合の再開は「3-1」を参照。

### 5. 作業

1. issueの内容を調査する。
2. 調査結果と実装方針が固まったらstatusを`implementing`へ更新する（通知なし）。
3. 対象リポジトリで作業用ブランチを作成する。
4. 実装する。まとまった進展があればsummaryとnext-actionを上書きする。
5. テスト・レビューへ移る時点でstatusを`verifying`へ更新する（通知なし）。summaryには実装済み内容、next-actionには検証項目を記録する。
6. pushし、PRを作成する（**マージはしない**）。
7. issueにPRへのリンクとテスト結果をコメントする。
8. `kobito:in-progress`ラベルを`kobito:pr-open`など作業完了を示すラベルに更新する。
9. statusを`pr_open`へ更新し、issue URL・PR URLを`related-link`へ含めて`--notify`する。summaryには変更概要とテスト結果、next-actionにはユーザーへ期待するレビュー内容を記録する。

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

合わせてstatusを`decision_pending`へ更新し、issue URLと判断材料のGitHub URL等を含めて`--notify`する。復旧不能な実行エラーで、ユーザー判断では再開できない場合は`failed`として`--notify`する。

## 認可・安全性の前提

v1のkobitoが読み取る入力元は、issueトラッカー（ユーザーが管理するワークスペース）とhuman-agent-boardの`user-to-agent`キュー（ユーザー自身が書き込む場所）に限られる。外部からの未検証な入力（Webhook等、第三者が任意の内容を書き込める経路）は扱わない。「誰の指示で再開してよいか」の照合は、この入力元の限定によって担保する。

**例外（LINE Bot経由の承認/却下、2026-08-18〜）**: `human-agent-board`の`agent-to-user`書き込み時、`ai-gateway`の`/line/webhook`経由でLINEへ通知が送られ、ユーザーはLINE上のボタンから承認/却下できる（FEZ-91）。このルートは技術的にはWebhookだが、(1) LINEのX-Line-Signatureによる署名検証、(2) 送信元LINE userIdが固定の`LINE_AUTHORIZED_USER_ID`（ユーザー本人）と一致することの確認、の二重チェックにより、実質的に「ユーザー自身が書き込む」のと同じ信頼境界を保っている。第三者が任意の内容を注入できる経路ではないため、上記の原則には抵触しない。詳細: `human-agent-board`リポジトリの`ai-gateway`側実装（`src/routes/line_webhook.py`）。

## 対象外（v1のスコープ外）

- マージ・デプロイなど、PR作成より先の操作
- issueトラッカー・human-agent-board（LINE Bot経由の承認/却下を含む）以外からの指示の受け付け
