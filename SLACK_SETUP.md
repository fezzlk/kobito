# Projectチャンネルでの会話・置き手紙を有効にする

Linearで作成済みのProjectと、そのProjectに接続されたSlackチャンネルを使う。
共通のkobito連絡チャンネルは作らない。Linearのチャンネル自動作成と、担当kobitoの会話受付を組み合わせる。

## 前提

- human-agent-boardのProject指定・通知command・Slack承認対応版、kobito本体、ai-gatewayのProject実行対応版を反映する。
- 受付は既存Mac上で一つだけ動かす。Socket Modeなので新しい公開Webhookやクラウドサービスは不要。
- 即時会話は、受付がMac上で`project_slack.py exec --mode conversation`を直接起動して実行する（Gatewayを経由しない、FEZ-281）。MacのClaude CLIが`--restricted`をサポートすることを確認する。claudeの場所は`projects.json`の`claude_bin`で指定する（既定`~/.local/bin/claude`）。
- この文書の操作でCloud Runを自動デプロイしたりはしない。Gatewayの反映には既存cloudbuild.yamlと既存トリガーを使い、費用影響を確認してから進める。

## Slackアプリを一つ用意する

1. https://api.slack.com/apps でCreate New App → From a manifestを選ぶ。
2. 個人用ワークスペースを選び、[templates/slack-app-manifest.json](templates/slack-app-manifest.json)を読み込む。アプリ名はkobito。
3. 表示される権限を確認してワークスペースへインストールする。既存のLinearアプリはそのまま使う。
4. OAuth & PermissionsのBot User OAuth Token（`xoxb-...`）をローカルに保管する。
5. Basic InformationのApp-Level Tokensで`connections:write`を持つトークン（`xapp-...`）を作る。Socket ModeがONであることを確認する。
6. 各Projectのチャンネルへkobitoアプリを招待する。Linearがチャンネルを作っただけでは、別アプリであるkobitoの参加までは成立しない。

トークンはこのチャット、issue、PR、リポジトリへ貼らない。下記の私有ファイルへ保存する。

## Macの私有設定

Python 3.8以上を使う。依存はSlack SDKとPyYAMLだけで、通常のkobitoの手順には追加インストールを要求しない。

```bash
cd ~/repos/kobito
python3 -m venv .venv-slack
.venv-slack/bin/python -m pip install -r requirements-slack.txt
mkdir -p ~/.config/kobito
chmod 700 ~/.config/kobito
cp templates/projects.example.json ~/.config/kobito/projects.json
chmod 600 ~/.config/kobito/projects.json
touch ~/.config/kobito/secrets.env
chmod 600 ~/.config/kobito/secrets.env
```

`secrets.env`をローカルエディタで開き、値を設定する。シェルの`source`では実行せず、受付が許可した次の項目だけを読み込む（`LINEAR_API_KEY`は優先度トリアージを使うときだけ）。引用符や`export`は付けない。

```text
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
AI_GATEWAY_URL=https://your-existing-gateway
AI_GATEWAY_SHARED_TOKEN=...
LINEAR_API_KEY=lin_api_...
```

`AI_GATEWAY_URL`は既存サービスのベースURL。新規サービスを作らない。受付は即時会話にGatewayを使わないが、手動での実行確認などのために残している。
`projects.json`にはworkspace ID、許可する本人のSlackメンバーID、各Linear ProjectのID・repo・URLを設定する。Projectに属さない通知の送り先は`default_project_id`（有効なProjectのID）で指定する。
IDは表示名から推測しない。次の読み取りコマンドで、Botトークンが接続するworkspace IDと名前を確認できる。

```bash
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py identity
```

出力の`bot_user_id`はアプリ自身のIDであり、本人の許可IDには使わない。本人のメンバーIDをSlackのプロフィールで確認し、`authorized_user_ids`へ入れる。
workspace IDが異なるトークンや、別workspaceの状態DBを使うと受付は停止する。

## Projectとチャンネルの対応

登録には既存Linear ProjectのUUID、対応するrepo名、正確なProject URLを使う。Projectを作り直したりissueを再起票したりしない。
`enabled`と`work_enabled`は初期状態でfalse。

新規Projectを登録する例:

```bash
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py register \
  --project-id project-uuid --repo example --name Example \
  --linear-url https://linear.app/example/project/example-project
```

Linearが付けたProjectへのブックマークから、既存チャンネルとの対応候補を取得する。

```bash
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py discover
```

`matched`は一つのProject URLに一つのチャンネル、`ambiguous`は複数候補、`not_found`は未検出。
招待・権限不足でブックマークを読めなかったチャンネルも出力する。見つからないものを名前の類似だけで結び付けない。

確認した対応を適用し、即時会話・置き手紙だけを有効にするには以下を使う。

```bash
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py discover --apply --enable-conversations
```

一意に対応した登録済みProjectだけが有効になる。`work_enabled`は変更しない。
すでに設定したチャンネルと異なる候補が見つかった場合は自動で付け替えない。
新規Linear Projectのチャンネル作成は既存の自動設定を使い、Projectの登録とチャンネルへのkobito招待を追加のオンボーディング手順とする。

## 受付の起動と確認

```bash
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py validate
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py serve
```

まず一つのProjectのチャンネルで確認する。

- `@kobito このプロジェクトの状況を教えて` → そのスレッドで会話する。コード・issue・設定は変更しない。
- `@kobito 置き手紙: 次の定期実行で調べてほしい内容` → 受付を返し、Project付きでBoardへ保存する。
- 会話中のスレッドへの本人の返信は、メンションを繰り返さなくても受け付ける。
- kobitoの質問に`A. 〜`形式の選択肢があると、選択肢ごとの返信用メッセージ（`置き手紙: A（〜）`）が同じスレッドに付く。コピーして貼ると置き手紙としてBoardに保存され、次の定期実行で読まれる。この質問には承認・却下ボタンを付けない。先頭の「置き手紙:」がない返信は即時会話になり、作業側には届かない。
- 計画の承認ボタン、または一つの計画だけがあるスレッドへの「承認」「却下」を確認する。

即時会話は既定で全Project合計12回/時まで。置き手紙はこの枠を使わない。
Macが停止中のトップレベル投稿は、起動後と60秒ごとの履歴取得で回収する。初回の履歴取得は直近24時間、以降は保存した位置から追う。
停止中のスレッド内返信は履歴回収の保証対象にしない。後で確実に届けたい依頼は、メンション付きのトップレベルの置き手紙にする。
Slack無料版で参照できない古い投稿は復元できない。

診断はメッセージ本文やトークンを出力しない。

```bash
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py inbox
~/repos/kobito/.venv-slack/bin/python ~/repos/kobito/scripts/project_slack.py retry '表示されたevent_key'
```

失敗した依頼はローカルのSQLiteに残る。再処理は同じイベントIDを使い、置き手紙や承認の二重作成を防ぐ。
受付が再起動しても、保存済みの返信があればAIを呼び直さず送信を再開する。Slack送信直後にプロセスが落ちた場合、返信が重複する可能性はあるが、承認・依頼の再実行はBoardのreceiptで抑止する。
Boardからの計画・状態通知も送信待ちを保存し、接続が戻れば受付が60秒ごとに再送する。送信済みの同じ内容は省き、改訂された計画は新しい承認対象として送る。

## 定期実行への切り替え

Macログイン時の自動受付には、手元での往復確認後、`launch-agent --output <保存先.plist>`でLaunchAgent設定を作成できる。このコマンドは設定を出力するだけで、インストールも起動もしない。
生成した設定はトークンを含まず、私有設定のパスを参照する。運用するチェックアウトで作成し、`~/Library/LaunchAgents/com.fezzlk.kobito-project-slack.plist`へ配置して読み込む。別の受付プロセスは先に終了する。Macのスリープ中は即時返信できない。

Project別実行の反映と動作確認が終わった後、既存Gatewayの`KOBITO_PROJECT_DISPATCH_ENABLED`をtrueにし、試行するProjectだけ`work_enabled`をtrueにする。
新しいSchedulerやVMを作らず、従来の実行枠をProject間で配分する。次の読み取りで選ばれるProjectを確認できる（選択位置は次へ進む）。

```bash
python3 ~/repos/kobito/scripts/project_slack.py next-project
```

既存の無指定Board CLIの`list`はProject付きの依頼を表示しない。全体を見る管理用の読み取りだけ`--all-projects`を使う。
古いProject未設定の依頼・statusは、Linearの所属と計画を確認して移行する。実行中のworktree、ブランチ、既存PRを捨てない。

LINE通知は廃止した（FEZ-273）。Board の通知は既定で `HUMAN_AGENT_NOTIFICATION_COMMAND`（このリポジトリの `project_slack.py notify`）に渡る。Project実行では受付がこの値を設定する。それ以外（全体実行・Claude/Codexの作業）では`~/.zshenv`で設定する。Projectに属さない項目は`default_project_id`のチャンネルに届く。未設定で送れなかった通知はBoardの未送信一覧に残り、受付が1分ごとに再送する。
優先度トリアージのボタン（`PRIORITY_TRIAGE.md`）を使うには、`secrets.env`に`LINEAR_API_KEY`を追加する。値は会話・issue・リポジトリに貼らない。

## 費用と参照

Slack・Linearの無料プラン内で使う構成。新しい有料API契約やクラウド常駐サービスは追加しない。
会話は既存のClaude利用枠を消費するため、既存の総利用量が0円になるという意味ではない。Cloud Runの本番設定・実行枠を変える前に費用を確認する。

- [LinearのProjectチャンネル自動作成](https://linear.app/docs/slack#project-slack-channels)
- [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/)
- [Slack Python SDK](https://github.com/slackapi/python-slack-sdk)
