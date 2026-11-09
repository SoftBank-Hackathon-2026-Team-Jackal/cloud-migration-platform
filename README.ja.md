<p align="center"><a href="README.md">English</a> · <b>日本語</b> · <a href="README.ko.md">한국어</a></p>

<div align="center">

# Cloud Migration Platform

AWS EKS で動いているアプリと PostgreSQL を、オンプレミスの K3s や別のクラウドへ移行するプラットフォームです。

![Python](https://img.shields.io/badge/Python_3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Temporal](https://img.shields.io/badge/Temporal-000000?logo=temporal&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-232F3E?logo=amazonwebservices&logoColor=white)
![Google Cloud](https://img.shields.io/badge/Google_Cloud-4285F4?logo=googlecloud&logoColor=white)
![Kubernetes](https://img.shields.io/badge/K3s·EKS·GKE-326CE5?logo=kubernetes&logoColor=white)
![WireGuard](https://img.shields.io/badge/WireGuard-88171A?logo=wireguard&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-D97757?logo=anthropic&logoColor=white)

</div>

## リポジトリ一覧

この README は `cloud-migration-platform` の説明です。チームのリポジトリは次のとおりです。

| リポジトリ | 役割 |
| --- | --- |
| [cloud-migration-platform](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform) | バックエンド：FastAPI API、Temporal Worker、移行ロジック（このリポジトリ） |
| [cloud-migration-web](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-web) | Web 画面（TypeScript）。各段階を音声で案内するジャッカル（Jackal）が付いています。既定ではモックデータで動き、SSE モードではプラットフォームのイベントで動きます |
| [demo-app](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/demo-app) | デモで使う移行対象アプリ：メッセージごとにどの環境と DB が処理したかを残す Go のチャットアプリ |
| [demo-app-vuln](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/demo-app-vuln) | `demo-app` に SQL インジェクションの脆弱性を 3 つ入れた版。Sandbox レッドチームの確認用 |
| [demo-application](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/demo-application) | 以前のデモ対象アプリ（Python）。現在は `demo-app` に置き換えました |
| [Softbank-Hackathon-2026-Finals](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/Softbank-Hackathon-2026-Finals) | 決勝の記録、PoC、クラウド設定（Terraform） |
| [.github](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/.github) | Organization のプロフィール |

<p align="center">
  <img src="docs/readme/clips/03-traffic-shift.gif" width="100%" alt="Web 画面でのトラフィック切替"><br>
  <sub>Web 画面（モックデータで撮影、移行先：オンプレミス K3s）</sub>
</p>

## 概要

ユーザーは Web 画面で GitHub リポジトリ、`.env` ファイル、AWS アカウントを接続し、移行する範囲を選びます。その後はプラットフォームが進めます。

1. ソースコード、EKS のマニフェスト、AWS リソースを読み、アプリが何を使っているかを整理し、人が結果をレビューします。
2. イメージをビルドし、隔離されたサンドボックスで動かしてテストします。テストが失敗すると AI（Claude）が原因を直し、もう一度テストします。続いて AI レッドチームがサンドボックスのコピーを攻撃し、見つかった弱点を直してから再確認します。
3. 移行先に合わせたデプロイ計画を作り、人が承認したらデプロイします。オンプレミスの場合は、ユーザーのクラスターに入れた Agent がデプロイします。
4. DB は論理レプリケーションで移し、シャドーリハーサルで実際のリクエストを移行先にも送って比較したあと、短時間だけ書き込みを止めて切り替えます。トラフィックは段階的に移します。

移行の途中で移行元のセキュリティ設定が抜け落ちないことを重視しました。移行元のセキュリティグループとマニフェストからコンポーネント間の接続関係を取り出し、移行先では既定で遮断し必要な経路だけを開ける NetworkPolicy として作り直します。

## 実行の流れ

<p align="center">
  <img src="docs/readme/temporal-flow.ja.svg" alt="移行1件の流れ: 分析から完了までの10段階" width="100%">
</p>

移行1件は [Temporal](https://temporal.io) のワークフロー `MigrationWorkflow` 1つとして実行されます。承認待ちのように時間のかかる段階があり、その間にサーバーが再起動することもあるため Temporal を選びました。ワークフローは履歴をもとに、止まったところから再開します。

| 構成要素 | このリポジトリでは |
| --- | --- |
| Workflow | `MigrationWorkflow` が分析、サンドボックス、承認、データ、トラフィックの各段階を子ワークフローとして順に実行 |
| Activity | git clone、AWS の参照、buildx、kubectl・Helm、DB 操作など外部とやり取りする処理。失敗すると自動で再試行 |
| Worker | プラットフォーム VM の `worker` コンテナが 6 つのタスクキューを、中継サーバーの `relay-worker` が `relay-1` を処理 |
| Signal · Query | 承認（`approve`）、DB 切替（`switchover`）、トラフィックのロールバック・再試行 / 状態参照（`status`） |

各段階の進行イベント（`analysis.*`、`sandbox.*`、`plan.*`、`provision.*`、`data.*`、`traffic.*`、`cutover.*`）は保存され、Web は `GET /migrations/{id}/events`（SSE）で受け取って画面に表示します。

## 各段階の説明

<details open>
<summary><b>1. 接続と分析</b></summary>

```mermaid
flowchart TB
  U[ユーザー] -->|リポジトリ・コミット / .env / AWS アカウント| W[Web]
  W --> API[API]
  API -->|.env は暗号化保存、以降は参照のみ| ENV[(env ストア)]
  API --> AN[AnalysisWorkflow]
  AN --> F[fetch_source<br/>固定コミットで clone]
  AN --> C[scan_code<br/>DB ドライバー・環境変数・ルート]
  AN --> M[scan_manifests<br/>Deployment・Service・NetworkPolicy]
  AN --> I[scan_aws_infra<br/>VPC・セキュリティグループ・LB・IAM・CloudTrail]
  F & C & M & I --> R[中間仕様]
  R --> AI[Claude マッピング<br/>標準リソースへの対応]
```

- AWS アカウントは CloudFormation で作成した読み取り専用ロールで接続します。移行範囲は EKS の Namespace とワークロード単位で選びます。
- `.env` は暗号化して保存し、分析ではキー名だけを見ます。
- 分析結果（中間仕様）には、アプリが使う DB、設定、外部公開、接続関係と、それぞれをプラットフォームの標準リソースにどう置き換えるかが入ります。

<p align="center">
  <img src="docs/readme/screens/03-scope.png" width="49%" alt="移行範囲の選択">
  <img src="docs/readme/screens/04-analysis.png" width="49%" alt="インフラとソースコードの分析">
</p>

</details>

<details>
<summary><b>2. サンドボックス前のレビュー</b></summary>

```mermaid
flowchart LR
  R[分析結果] --> G[レビューゲート]
  G --> M[項目ごとの対応<br/>そのまま移行 · API の違い · 失われる機能]
  G --> S[移行元のセキュリティ点検<br/>外部 HTTPS · DB アクセス範囲<br/>DB 通信の暗号化 · 管理 API の公開]
  M & S --> U[ユーザーが移行対象を選び<br/>失われる機能を承認]
  U --> NEXT[サンドボックス]
```

- ビルドの前に、人が分析結果をレビューします。項目ごとに、そのまま移せるか、移行先で API が変わるか、失われる機能（例：WAF 連携などの ALB 専用機能）は何かを示します。
- 移行元インフラの外部 HTTPS、DB アクセス範囲、DB 通信の暗号化、管理 API の公開を点検し、移行後のセキュリティが移行元と同じか、強くなるかを項目ごとに示します。
- ユーザーは移行対象ごとに移すかどうかを選び、失われる機能を確認して承認します。レビューを通った対応だけがサンドボックスに進みます。

</details>

<details>
<summary><b>3. イメージビルド</b></summary>

```mermaid
flowchart LR
  SRC[固定したコミット] -->|アップロード| S3[(S3: ソース・ログ)]
  S3 --> BX[プラットフォーム VM の Docker<br/>buildx · amd64+arm64]
  BX -->|push| ECR[(ECR)]
  ECR -->|sha256 digest| NEXT[以降は digest だけでイメージを指す]
```

- プラットフォーム VM の Docker で buildx を使って amd64・arm64 のイメージをビルドし、ECR に push します。ビルド中はインスタンスメタデータへのアクセスを遮断し、ユーザーのコードがプラットフォームの認証情報を読めないようにしています。
- 以降の段階はタグではなくイメージの digest を使います。サンドボックスでテストしたイメージがそのままデプロイされます。

</details>

<details>
<summary><b>4. サンドボックス検証とレッドチーム</b></summary>

```mermaid
flowchart LR
  REQ[中間仕様 + digest] --> RD[共通レンダラー]
  RD --> K3D[k3d サンドボックス<br/>一時 Namespace · 許可リスト型の出口プロキシ · 2時間後に削除]
  K3D --> T[テスト<br/>ヘルス · DB 書き込み→読み出し · 再起動後も保持<br/>+ AI 生成テスト]
  T -->|失敗| FX[AI Fixer が修正] -->|最大3回| RD
  T -->|合格| RT[AI レッドチーム<br/>隔離された攻撃 Pod]
  RT -->|弱点を発見| FX
  RT -->|残る弱点なし| OK[PASSED]
```

- プラットフォーム VM 上の k3d クラスターに、実行ごとに一時的な Namespace を作ります。2 時間たつと削除します。
- 外向きの通信は許可リスト型の出口プロキシだけを通ります。分析で見つかった外部ホストと、移行元に残すサービス（例：S3）だけを開け、それ以外は遮断して記録します。
- 標準テストはヘルスチェック、DB への書き込みと読み出し、再起動後のデータ保持です。これに AI が作ったテストが加わります。
- 失敗すると原因を分類（仕様、コード、ビルド、レンダラー、環境）し、Fixer が設定やコードを修正してから再度レンダリングしてテストします。最大 3 回まで試します。
- サンドボックスと実際のデプロイは同じレンダラー（`shared_deployment_v2`）を使います。

**AI レッドチーム**（[ADR-008](docs/decisions/ADR-008.md)）

- テストに合格すると、AI エージェントがサンドボックスのアプリのコピーを攻撃します。攻撃 Pod はアプリのポートにしか届かず、DNS、DB、他の Namespace、インターネット、クラスター API には届きません。
- モデルが決めるのは次に送るリクエストだけで、ツール名とパスの形で出します。出力にホストや URL の項目がないため、別の宛先を選べません。
- 同意は移行を始めるときに一度だけもらいます。既定の強度は観察のみ（データを変えない）、時間の上限は 5 分で、点検中は外部への通信を切ります。
- 弱点のエンドポイント、証拠、再現手順は、実際に送ったリクエストと実際の応答からだけ取り、秘密情報は伏せます。
- 弱点は Fixer がコードを直し、再ビルドしたイメージに同じリクエストを送って再検証します。セキュリティ修正は最大 3 回で、イメージ digest ごとに追跡します。残った弱点は表示と記録だけで、移行は止めません。
- 結果はソースのハッシュをキーに S3 にキャッシュし、同じソースを再び移すときに同じ攻撃をやり直しません。

<p align="center">
  <img src="docs/readme/screens/06a-redteam-fixing.png" width="49%" alt="レッドチームの弱点を修正中">
  <img src="docs/readme/screens/06b-redteam-done.png" width="49%" alt="レッドチーム完了">
</p>

</details>

<details>
<summary><b>5. デプロイ計画と承認</b></summary>

```mermaid
sequenceDiagram
  participant WF as MigrationWorkflow
  participant P as Plan 公開
  participant A as 承認者（Web）
  WF->>P: 中間仕様 + 移行先プロファイル（オンプレミス / AWS / GCP）
  P->>P: Namespace · NetworkPolicy · DB · App に変換
  P-->>A: プレビュー（manifest digest）
  A->>WF: 承認（特定の Revision）
  WF->>WF: 実行時に承認済み digest を再照合
```

- 移行先のプロファイル（オンプレミス、AWS、GCP）に合わせて Namespace、NetworkPolicy、DB、アプリの構成を作り、Revision として保存します。
- 承認は Revision 単位です。デプロイ時のイメージ digest が承認時と異なる場合は `ARTIFACT_DIGEST_MISMATCH` で中止します。

<p align="center">
  <img src="docs/readme/screens/07-plan.png" width="49%" alt="デプロイ計画">
  <img src="docs/readme/screens/08-approval.png" width="49%" alt="承認">
</p>

</details>

<details>
<summary><b>6. オンプレミスへのデプロイ</b></summary>

```mermaid
sequenceDiagram
  participant AG as Agent（ユーザーの K3s）
  participant API as プラットフォーム API
  participant RL as 中継サーバー
  AG->>API: ワンタイムトークンで登録 → 専用の認証情報
  AG->>API: WireGuard 公開鍵を登録 → トンネル IP
  RL->>API: peer 一覧を同期
  AG-->>RL: 外向きにハンドシェイク（ユーザー側の受信ポート不要）
  AG->>API: ポーリング: 承認済みジョブを受信
  AG->>AG: Namespace → NetworkPolicy → DB → App
  AG->>API: 進行・結果を報告 → Pod · DB · アプリ · トンネル経由ヘルスを確認
```

- ユーザーは Agent の Helm チャートを一度インストールするだけです。Agent はワンタイムトークンで登録し、プラットフォームへの外向き接続だけを行います。
- WireGuard トンネルも Agent から中継サーバーへ接続するため、ユーザーのネットワークでポートを開ける必要はありません。
- Agent は承認済みのジョブをポーリングで受け取り、Namespace、NetworkPolicy、DB、アプリの順にデプロイして結果を報告します。失敗した場合は直前の Revision に戻します。
- Agent は GitHub Release と OCI の Helm チャートで配布し、イメージは digest で固定しています（現在 0.6.0）。

<p align="center">
  <img src="docs/readme/screens/10-provisioning-done.png" width="100%" alt="オンプレミスへのデプロイ完了">
</p>

</details>

<details>
<summary><b>7. データ複製、シャドーリハーサル、DB 切替</b></summary>

```mermaid
flowchart LR
  SRC[(移行元 DB)] -->|論理レプリケーション| GW[移行元側の固定ゲートウェイ · NLB]
  GW -->|WireGuard データトンネル| TGT[(移行先 DB)]
  TGT --> ST[変更分に追いつく]
  ST --> SH[シャドーリハーサル<br/>実際の GET を複製 · 比較]
  SH --> AP{DB 切替の承認}
  AP --> PB[PgBouncer で書き込みを一時待機<br/>シーケンス同期 · 行数・チェックサム検証]
  PB --> SW[書き込み先を移行先へ]
  SW --> RV[逆方向レプリケーション<br/>移行先 → 移行元、切り戻し用]
```

- 最初にネットワークの事前点検を行います。データトンネルは移行元側の固定ゲートウェイと NLB から入り、実行ごとに自分の peer をつなぎます（[ADR-007](docs/decisions/ADR-007.md)）。別の実行がゲートウェイを使っている場合は、横取りせず `TUNNEL_IN_USE` で止まります。
- スキーマと初期データをコピーしたあと、論理レプリケーションで変更分に追いつきます。移行先のアプリがすでに作ったテーブルはそのまま残し、足りないテーブルだけを復元します。
- シャドーリハーサル（[ADR-013](docs/decisions/ADR-013.md)）：レプリケーションが追いつくと、Envoy が許可したパスの実際の GET リクエストを移行先にも複製します。移行元 2 回と移行先 1 回の応答を比べて、ノイズになる項目を見分けます。結果は参考で切替を止めず、不一致は AI が説明します。
- DB の切り替えは 2 回目の承認のあとに行います。PgBouncer で書き込みを一時停止し、シーケンスを合わせ、行数とチェックサムを比較してから移行先 DB に切り替えます。
- 切替後は移行先から移行元への逆方向レプリケーションを保ち、切り戻せるようにします。
- EKS から GKE への検証では、書き込みの停止は 83ms で、行数とチェックサムは一致しました。

<p align="center">
  <img src="docs/readme/clips/01-data-migration.gif" width="49%" alt="データ複製">
  <img src="docs/readme/clips/02-db-cutover.gif" width="49%" alt="DB 切替">
</p>

</details>

<details>
<summary><b>8. トラフィック切替</b></summary>

```mermaid
flowchart LR
  USER[ユーザーのリクエスト] --> EV[Envoy 中継サーバー]
  EV --> O[移行元]
  EV -->|10% → 50% → 100%| T[移行先 · トンネル経由]
  EV --> G{段階ごとに観察<br/>リクエスト数 · 失敗 · 遅延}
  G -->|合格| NEXT[次の段階]
  G -->|失敗| RB[自動ロールバック → 移行元 100%]
```

- 中継サーバーの Envoy の重みを 10%、50%、100% の順に変えます。各段階でリクエスト数、失敗、遅延を見て、基準を超えたら移行元に戻します。
- Web からロールバックや再試行を実行することもできます。
- 手動の段階モードでは、段階ごとに「次の段階」を押すか `POST /migrations/{id}/traffic/advance` を呼ぶまで進みません。
- デモや確認のために、移行先への経路に障害（遮断、拒否、パケット損失）を入れて自動ロールバックを見せられます。

<p align="center">
  <img src="docs/readme/screens/18-report.png" width="100%" alt="切替後の移行レポート">
</p>

</details>

## アプリの観測

- 移行したアプリは、移行元と移行先の両方のクラスターから OpenTelemetry（OTLP/HTTP）をプラットフォームに送ります。
- Caddy は取り込みトークンの付いた POST だけを受け付けます。メトリクスは Prometheus に、トレースは Jaeger に送ります。
- Web のトラフィック切替・DB 切替の画面にパネルとして表示し、環境ごとにアプリ自身の遅延やエラーを比べられます。

## アーキテクチャ

```mermaid
flowchart TB
  subgraph WEB[Web]
    UI[cloud-migration-web]
  end
  subgraph CP[プラットフォーム VM · EC2]
    EDGE[Caddy] --> API[FastAPI]
    API --> PDB[(PostgreSQL)]
    API --> TS[Temporal]
    WK[Worker] --> TS
    WK --> DK[Docker buildx]
    WK --> SB[k3d サンドボックス]
  end
  subgraph SRC[移行元 · AWS]
    EKS[EKS アプリ · DB]
    ECR[(ECR)]
    S3[(S3)]
  end
  subgraph RELAY[中継サーバー · EC2]
    ENV[Envoy] --- RW[relay-worker]
    WG[WireGuard]
  end
  subgraph DST[移行先]
    AG[On-prem Agent] --> K3S[K3s アプリ · DB]
    GKE[GKE · AWS]
  end
  UI --> EDGE
  WK -->|読み取り専用ロール| EKS
  DK --> ECR
  WK --> S3
  RW --> TS
  AG -->|ポーリング| EDGE
  AG <-->|トンネル| WG
  ENV --> EKS
  ENV -->|トンネル| K3S
```

## 使用しているクラウドサービス

| クラウド | サービス | 用途 |
| --- | --- | --- |
| AWS | EC2、SSM、CloudFormation | プラットフォームと中継サーバーの運用、デプロイ、接続ロールのテンプレート |
| AWS | EKS、IAM（ロール・アクセスエントリ）、CloudTrail、VPC | 移行元の分析（読み取り専用）、データ移行用の別ロール |
| AWS | ECR、S3 | ビルドしたイメージ、ソースとビルドログ |
| Google Cloud | GKE、Terraform | クラウドへのデプロイ、データ移行の検証 |
| オンプレミス | K3s、Helm、WireGuard | Agent のデプロイ、トンネル |

## AI の利用範囲

AI には Claude を使っています。使っているのは次の 3 か所です。

- 分析：移行元の構成をプラットフォームの標準リソースに対応付けます。判断ごとに根拠（ファイルと行番号）を残し、人が計画のプレビューで確認します。
- サンドボックス：テストを作り、失敗したテストの原因を直し、サンドボックスのコピーをレッドチームで攻撃し、シャドーリハーサルの不一致を説明します。テスト、修正、攻撃は、外向きの通信を許可リストに絞ったサンドボックスの中だけで動きます。
- コストとデータ：移行 1 件あたり $5、1 日 $20、1 か月 $100 の上限をコードで適用しています。モデルにはマスクした根拠の一部だけを送り、`.env` の値やコード全体は送りません。詳しくは [ADR-004](docs/decisions/ADR-004.md) を参照してください。

## 実装状況

デモサーバーで実際に動いている範囲です。

| 段階 | 状態 |
| --- | --- |
| 接続・範囲選択、分析、レビュー、イメージビルド、サンドボックス検証 | 動作 |
| サンドボックスのレッドチーム（攻撃、修正、再検証） | 動作 |
| デプロイ計画・承認、オンプレミスへのデプロイ（Agent・トンネル） | 動作 |
| データ複製、シャドーリハーサル、DB 切替 | EKS→オンプレミス K3s、EKS→GKE ともに動作 |
| トラフィック切替 | 動作。オンプレミス K3s まで最後まで進んだ実際の実行で確認 |

## 主な設計判断

- Temporal を選んだ理由と、実行中のワークフローを保ったままコードを変更する方法：[ADR-001](docs/decisions/ADR-001.md)
- 最初の Provisioning 作業の範囲と、並行作業の分け方：[ADR-002](docs/decisions/ADR-002.md)
- サンドボックスで検証したイメージ digest を本番のデプロイにそのまま使う方式：[ADR-003](docs/decisions/ADR-003.md)
- AI のコスト上限と、モデルに送ってよいデータ：[ADR-004](docs/decisions/ADR-004.md)
- 移行元・移行先環境の接続方式（AWS のロール委任、GCP の固定プロジェクト）：[ADR-005](docs/decisions/ADR-005.md)
- トラフィック切替を Envoy の中継で行う理由：[ADR-006](docs/decisions/ADR-006.md)
- オンプレミスのデータ移行経路を移行元側のゲートウェイにした理由：[ADR-007](docs/decisions/ADR-007.md)
- サンドボックスのレッドチームを安全に動かす方式（隔離、同意、修正後の再検証）：[ADR-008](docs/decisions/ADR-008.md)
- プラットフォームを FastAPI API と Temporal Worker で構成した理由：[ADR-009](docs/decisions/ADR-009.md)
- データを PostgreSQL の論理レプリケーションで移し、移行元側の PgBouncer で切り替える理由：[ADR-010](docs/decisions/ADR-010.md)
- Worker が `kubectl exec` で SQL を実行し、EKS トークンを Activity 間で再利用する理由：[ADR-011](docs/decisions/ADR-011.md)
- 切替時に移行元アプリの NetworkPolicy へ許可ルールだけを追加する理由：[ADR-012](docs/decisions/ADR-012.md)
- DB 切替の承認前にシャドーリハーサルで移行先の応答を観察する理由：[ADR-013](docs/decisions/ADR-013.md)
- デモ用プラットフォームを単一の EC2 で運用し、main のコミットだけをデプロイする方式：[ADR-014](docs/decisions/ADR-014.md)

秘密情報はリポジトリ、イベント、ログに残しません。`.env` は暗号化して保存し以降は参照だけを渡します。Agent と承認者のトークンは DB にハッシュだけを保存します。

## 開発の進め方

- 判断は `docs/decisions/` の ADR 14 件と領域ごとの仕様（`specs/`）に残しました。作業は `tickets/` のチケット 154 件に分け、検討した代替案、選んだ理由、確認結果を一緒に書きました。
- 変更はすべて PR で出し、CI（ruff、pytest 約 3,900 件）を通してから squash マージしました。これまでにマージした PR は約 225 件です。
- 分析・検証、Provisioning、データ移行、トラフィック、Web で担当を分け、領域間のインターフェースは `packages/contracts` とイベントスキーマで合わせました。

## リポジトリ構成

| パス | 内容 |
| --- | --- |
| `apps/api` | FastAPI。移行の開始・参照・承認、接続、Agent・トンネル、イベント（SSE）、レポート |
| `apps/worker` | Temporal の Workflow、Activity、Worker |
| `packages/contracts` | イベント、タスクキュー、入力モデルの定義 |
| `packages/scanners` | コード、マニフェスト、AWS の分析 |
| `packages/agents` | AI マッピング、テスト生成、Fixer |
| `packages/testsuite` | サンドボックスのテスト実行 |
| `packages/providers` | レンダラー、NetworkPolicy、移行先ごとのデプロイ、オンプレミス Agent・トンネル |
| `packages/data` | データ複製、DB 切替 |
| `packages/traffic` | Envoy の設定、トラフィック切替 |
| `infra/` | デモサーバー、Agent の Helm チャート、中継サーバー、接続テンプレート、Terraform |
| `docs/`、`specs/`、`tickets/` | 設計判断、仕様、作業記録 |

## ローカルでの実行

Python 3.11、[uv](https://docs.astral.sh/uv/)、Docker Compose が必要です。詳しい実行方法、Agent の登録、ファイルごとの役割は[開発ガイド](docs/development.md)（韓国語）を参照してください。

```bash
cp .env.example .env
make setup
make infra-up    # PostgreSQL, Temporal
make workers     # 別のターミナルで
make api         # 別のターミナルで。http://localhost:8000/docs
```

## ドキュメント

設計ドキュメントの多くは韓国語で書かれています。

- [開発ガイド](docs/development.md)
- [設計判断（ADR）](docs/decisions/)
- [Temporal ガイド](docs/architecture/temporal-flow.md)
- [イベントガイド](docs/engineering/events-guide.md)
- [`.env` 参照ガイド](docs/engineering/env-file-guide.md)
- [オンプレミス Agent の導入と運用](specs/provisioning/onprem-agent-install.md)
- [デモサーバーの運用](infra/demo/README.md)

<!-- TODO: チーム紹介 -->
