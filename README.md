# CALC_POWER_SHAPING_FACTOR — Simple Version

## 1. 方針

既存実装の本番計算ロジックと出力数値を維持しつつ、研究用途・抽象化・重複入口を削除した簡素版です。

残した処理:

1. CSVまたはJERARMからのJEPXスポット取得
2. 複数Price列の縦持ち化
3. 年度・曜日・祝日・固定thetaの付与
4. 最新完了年度を使ったOOS窓長選択
5. シェイピング係数の最新窓による再推定
6. 将来日付×48コマへの係数割当
7. 3つのCSV出力
8. 既存`window_ranking`と同じ基本残差診断

削除した処理:

- `cli.py`と`batch_main.py`の重複
- `ShapingAnalyzer`ラッパークラス
- Protocol群
- theta全探索
- 詳細残差レポートAPI
- `maximum_theta_groups`設定
- ETRM分岐

## 2. ディレクトリ

```text
CALC_POWER_SHAPING_FACTOR_SIMPLE/
├── batch_main.py
├── README.md
├── conf/
│   └── batch_config.py
├── lib/
│   ├── analysis.py
│   ├── data_sources.py
│   └── holidays.py
├── sql/
│   └── jepx_spot_price_koma.sql
└── tests/
    └── test_smoke.py
```

## 3. 計算ロジック

```text
shaping_factor
= conditional_average(month, timeframe, theta_group)
  / monthly_average(month)
```

最新完了年度をテスト年度とし、直近1年、2年、...の学習窓をRMSEで比較します。最良窓の年数を最新年度へロールして係数を再推定し、将来カレンダーへ割り当てます。

## 4. 実行

CSV:

```bash
python batch_main.py input.csv \
  --data-source csv \
  --output-directory data \
  --price-columns SYSTEM_PRICE_VALUE PRICE_CHUBU_VALUE
```

JERARM:

```bash
python batch_main.py \
  --data-source jerarm \
  --sql-path sql/jepx_spot_price_koma.sql \
  --start-date 2020-04-01 \
  --end-date 2026-03-31 \
  --output-directory data
```

## 5. 出力

```text
shaping_window_ranking.csv
shaping_future_shaping_table.csv
shaping_future_shaping_summary.csv
```

## 6. テスト

```bash
pytest -q
```

`tests/test_smoke.py`は、係数式、年度分割、将来48コマ、全体実行を少量の決定論的データで確認します。

## 7. 数値整合性

簡素化で変更していない数値ロジック:

- Priceの縦持ち化
- 年度判定
- 学習年度のプール平均
- 月次平均
- シェイピング係数
- OOS再構成価格
- RMSE、MAE、Bias
- 窓ランキング順
- 最新窓へのロール
- 将来係数の結合キー
- 将来サマリー集計

既存結果との比較時は、浮動小数点を`rtol=0, atol=1e-12`で確認してください。

## 8. 注意点

- 年度完了判定は年度初日、年度末日、12か月を確認しますが、日別48コマの完全性までは確認しません。
- 将来月の曜日構成が過去と異なるため、将来係数の月平均は厳密に1にならない場合があります。
- 月次フォワードと厳密に整合させる場合は、下流で将来月ごとに再正規化してください。
- `minimum_observations=1`は既存結果維持のための既定値です。安定性を優先する場合は引き上げてください。

## 9. 変数命名規則

- `pandas.DataFrame`は原則として`df_<意味>`とする。
- 関数の主入力など、追加語が意味を重複させる場合は`df`とする。`df_data`は使用しない。
- `pandas.Series`は必要に応じて`s_<意味>`とする。
- 年度集合、列名集合、日付集合など、DataFrameでない値には`df_`を付けない。

## 10. Snowflakeへの格納

`lib/db_outputs.py`の`_insert_db()`は、計算済みDataFrameを`Jerarm.insertSfAna`へ渡します。計算ロジックから分離されているため、DB格納の有無は数値結果へ影響しません。

```python
from lib.db_outputs import _insert_db

_insert_db(
    df_future_shaping_table,
    "JEPX_FUTURE_SHAPING_TABLE",
    database_name="ANALYTICS",
    schema_name="RISK",
    overwrite=False,
)
```

チーム環境の`Jerarm.insertSfAna`のキーワード名が異なる場合は、`lib/db_outputs.py`の呼出し引数だけを調整してください。テストでは`insert_function`へFake関数を注入できるため、Snowflakeへ接続せず引数と非破壊性を確認できます。


## 12. ログ出力（V4）

`batch_main.py` は、社内環境では `Jerarm.get_logger()` を使用し、プロジェクト直下の `log/` へ実行ログを出力する。`jerarm` を利用できないCSV・テスト環境では、Python標準 `logging` の `FileHandler` へ自動的にフォールバックする。

主な記録内容:

- START / ENDおよび未処理例外のスタックトレース
- CSVまたはJERARM/Snowflakeの入力分岐
- 入力期間、SQLファイル、将来年度数、出力先
- 価格列の明示指定または自動検出分岐
- Price別の選択窓、lookback年数、テスト年度、RMSE
- 各出力ファイルの行数、列数、保存先

ログ処理は観測専用であり、DataFrameの値、計算順序、集計キー、CSV列および並び順を変更しない。

---

## 13. コアロジックの数理仕様

本章では、実装されているJEPXスポット価格シェイピングロジックを、再現可能な数式として定義する。数式は `lib/analysis.py` の処理順序に対応しており、年度分割、シェイピング係数推定、アウト・オブ・サンプル評価、ローリング窓選択、最新データによる再推定、および将来年度テーブル生成までを対象とする。

### 13.1 記号と添字

観測単位を半時間コマとし、各観測を次の記号で表す。

- $t$：観測行または受渡時点
- $d_t$：受渡日
- $m_t \in \{1,\ldots,12\}$：暦月
- $h_t \in \{1,\ldots,48\}$：30分コマ番号
- $y_t$：年度。年度開始月を $s$ とすると、$s=4$ が既定値
- $w_t$：曜日コード
- $q_t$：日種別。`HOL`, `SAT`, `SUN`, `MON`, `TUE`, `WED`, `THU`, `FRI` のいずれか
- $g_t=\theta(q_t)$：日種別を集約したthetaグループ
- $P_t$：対象価格列の実績価格
- $E$：係数推定に使用する年度集合
- $T$：アウト・オブ・サンプル評価年度
- $D_{E,m,h,g}$：年度集合 $E$ から推定した月×コマ×thetaグループ別シェイピング係数

既定のthetaグループ写像は次のとおりである。

$$
\theta(q)=
\begin{cases}
G0, & q\in\{HOL,SAT,SUN\},\\
G1, & q\in\{MON,FRI\},\\
G2, & q\in\{TUE,WED,THU\}.
\end{cases}
$$

祝日に該当する日は曜日よりも祝日判定を優先する。したがって、日種別は

$$
q_t=
\begin{cases}
HOL, & d_t\text{ が祝日集合に含まれる場合},\\
w_t, & \text{それ以外}
\end{cases}
$$

として定義される。

### 13.2 年度の定義

年度開始月を $s$ とする。受渡日 $d_t$ の年を $\mathrm{year}(d_t)$、月を $m_t$ とすると、年度は

$$ 
y_t = 
\begin{cases} 
\mathrm{year}(d_t), & m_t \ge s, \\ 
\mathrm{year}(d_t)-1, & m_t < s 
\end{cases} 
$$

既定値 $s=4$ の場合、2024年4月1日から2025年3月31日までの観測はFY2024に属する。

ある年度 $y$ は、次の条件をすべて満たす場合に「完全年度」とみなす。

1. 含まれる暦月数が12である。
2. 最初の観測日が年度開始日以前である。
3. 最後の観測日が年度終了日以後である。

年度開始日と終了日は

$$
B_y=\mathrm{Date}(y,s,1),
$$

$$
F_y=\mathrm{Date}(y+1,s,1)-1\text{ day}
$$

である。完全年度の昇順集合を

$$
\mathcal{Y}=\{y_{(1)},y_{(2)},\ldots,y_{(K)}\},
\qquad y_{(1)}<\cdots<y_{(K)}
$$

とする。最新の完全年度

$$
T=y_{(K)}
$$

をアウト・オブ・サンプル評価年度とし、それ以前を候補学習年度とする。この分割により、評価年度の情報は係数推定に使用されない。

### 13.3 ローリング学習窓

評価年度 $T$ より前に利用可能な完全年度を

$$
\mathcal{Y}_{\mathrm{train}}
=\{y_{(1)},\ldots,y_{(K-1)}\}
$$

とする。利用可能な学習年度数を $K-1$ とすると、長さ $L$ の候補窓は直近 $L$ 年からなる。

$$
E_L
=\{y_{(K-L)},\ldots,y_{(K-1)}\},
\qquad L=1,2,\ldots,K-1.
$$

実装上の窓名は `E1`, `E2`, ... である。例えば、評価年度がFY2025で、それ以前の完全年度がFY2020からFY2024の場合、候補は

$$
E_1=\{2024\},
$$

$$
E_2=\{2023,2024\},
$$

$$
\cdots
$$

$$
E_5=\{2020,2021,2022,2023,2024\}
$$

となる。

### 13.4 月平均価格

年度集合 $E$ と暦月 $m$ に対応する観測集合を

$$
\mathcal{I}_{E,m}
=\{t\mid y_t\in E,\ m_t=m\}
$$

とする。学習期間内の月平均価格は

$$
\bar P_{E,m}
=\frac{1}{N_{E,m}}
\sum_{t\in\mathcal{I}_{E,m}}P_t,
$$

$$
N_{E,m}=|\mathcal{I}_{E,m}|
$$

で定義する。

ここで、同じ暦月に属する複数年度の観測は、年度別平均を作ってから平均するのではなく、すべてのコマ観測をプールしたうえで一度だけ平均する。すなわち、本実装は

$$
\bar P_{E,m}
=\frac{\sum_{y\in E}\sum_{t:y_t=y,m_t=m}P_t}
{\sum_{y\in E}N_{y,m}}
$$

を用いる。一般に、年度別平均の単純平均

$$
\frac{1}{|E|}\sum_{y\in E}\bar P_{y,m}
$$

とは、年度ごとの有効観測数が異なる場合に一致しないため、両者を混同しないこと。

### 13.5 条件付き平均価格

月 $m$、コマ $h$、thetaグループ $g$ の条件付き観測集合を

$$
\mathcal{I}_{E,m,h,g}
=\{t\mid y_t\in E,\ m_t=m,\ h_t=h,\ g_t=g\}
$$

とする。条件付き平均価格は

$$
\bar P_{E,m,h,g}
=\frac{1}{N_{E,m,h,g}}
\sum_{t\in\mathcal{I}_{E,m,h,g}}P_t,
$$

$$
N_{E,m,h,g}
=|\mathcal{I}_{E,m,h,g}|
$$

である。`minimum_observations` を $N_{\min}$ とすると、係数を採用する条件は

$$
N_{E,m,h,g}\ge N_{\min}
$$

である。既定値は $N_{\min}=1$ である。

### 13.6 シェイピング係数

シェイピング係数は、条件付き平均価格を同じ学習年度集合・同じ暦月の月平均価格で除した相対係数として定義する。

$$
D_{E,m,h,g}
=\frac{\bar P_{E,m,h,g}}{\bar P_{E,m}}.
$$

したがって、$D_{E,m,h,g}>1$ は、当該月・コマ・thetaグループの平均価格が月全体の平均価格を上回ることを意味し、$D_{E,m,h,g}<1$ は下回ることを意味する。

この定義の重要な点は次のとおりである。

- 分子と分母は同一の学習年度集合 $E$ から計算する。
- 分母は月×コマ×thetaグループ別ではなく月別平均である。
- 年度ごとの係数を推定してから平均するのではなく、価格観測を年度横断でプールして推定する。
- 価格列ごとに独立して係数推定と窓選択を行う。

### 13.7 アウト・オブ・サンプル再構築

候補学習窓 $E_L$ の係数を、評価年度 $T$ の実績へ適用する。評価年度 $T$ の暦月 $m$ における実績月平均を

$$
\bar P_{T,m}
=\frac{1}{N_{T,m}}
\sum_{t:y_t=T,m_t=m}P_t
$$

とする。

評価年度内の各観測 $t$ に対する再構築価格は

$$
\widehat P_t^{(E_L)}
=\bar P_{T,m_t}\,D_{E_L,m_t,h_t,g_t}
$$

である。ここで評価年度の月平均 $\bar P_{T,m_t}$ は価格水準を与え、過去年度から推定した $D_{E_L,m_t,h_t,g_t}$ は月内・日種別・コマ別の相対形状を与える。

残差は

$$
e_t^{(E_L)}
=P_t-\widehat P_t^{(E_L)}
$$

で定義する。

係数テーブルとの結合は `(month, timeframe, theta_group)` をキーとする多対一結合である。対応係数が存在しない観測は欠損係数件数として数えたうえで、RMSEなどの評価対象から除外する。評価に利用可能な観測集合を

$$
\mathcal{V}_{E_L,T}
=\{t\mid y_t=T,\ D_{E_L,m_t,h_t,g_t}\text{ が存在する}\}
$$

とする。

### 13.8 評価指標

有効評価件数を

$$
N_L=|\mathcal{V}_{E_L,T}|
$$

とする。各候補窓のRMSEは

$$
\mathrm{RMSE}(E_L)
=\sqrt{\frac{1}{N_L}
\sum_{t\in\mathcal{V}_{E_L,T}}
\left(e_t^{(E_L)}\right)^2}
$$

である。

MAEは

$$
\mathrm{MAE}(E_L)
=\frac{1}{N_L}
\sum_{t\in\mathcal{V}_{E_L,T}}
\left|e_t^{(E_L)}\right|
$$

である。

バイアスは

$$
\mathrm{Bias}(E_L)
=\frac{1}{N_L}
\sum_{t\in\mathcal{V}_{E_L,T}}
e_t^{(E_L)}
$$

である。正のバイアスは、平均的に実績価格が再構築価格を上回ることを表す。

残差の標準偏差は、標本標準偏差として

$$
s_e
=\sqrt{\frac{1}{N_L-1}
\sum_{t\in\mathcal{V}_{E_L,T}}
\left(e_t^{(E_L)}-\bar e\right)^2}
$$

で計算する。

### 13.9 残差診断

残差平均は

$$
\bar e=\frac{1}{N_L}\sum_{t=1}^{N_L}e_t
$$

である。平均ゼロに対する通常のt統計量は

$$
t_{\mathrm{mean}}
=\frac{\bar e}{s_e/\sqrt{N_L}}
$$

である。

また、残差の時系列依存を確認するため、ラグ

$$
\ell\in\{1,2,48,336\}
$$

の自己相関を出力する。ラグ48は48コマ＝1日、ラグ336は48コマ×7日＝1週間に対応する。

ラグ $\ell$ の標本自己相関は概念的に

$$
\widehat\rho(\ell)
=\frac{\sum_{t=\ell+1}^{N_L}(e_t-\bar e)(e_{t-\ell}-\bar e)}
{\sum_{t=1}^{N_L}(e_t-\bar e)^2}
$$

で表される。

残差分布については歪度、超過尖度、Jarque–Bera統計量およびp値を出力する。歪度を $S$、通常の尖度を $K$ とすると、Jarque–Bera統計量は

$$
JB
=\frac{N_L}{6}
\left(
S^2+\frac{(K-3)^2}{4}
\right)
$$

で表される。

### 13.10 Newey–West HAC平均検定

半時間データでは残差に系列相関が残る可能性があるため、残差平均の標準誤差についてNewey–West型HAC補正も計算する。残差を中心化して

$$
u_t=e_t-\bar e
$$

とし、ラグ $\ell$ の自己共分散推定量を

$$
\widehat\gamma_{\ell}
=\frac{1}{N_L}
\sum_{t=\ell+1}^{N_L}u_tu_{t-\ell}
$$

とする。

帯域幅を $B$ とし、Bartlettウェイトを

$$
w_{\ell}=1-\frac{\ell}{B+1}
$$

とすると、長期分散推定量は

$$
\widehat\Omega_{NW}
=\widehat\gamma_0
+2\sum_{\ell=1}^{B}w_{\ell}\widehat\gamma_{\ell}
$$

である。ただし、実装上の有効帯域幅は

$$
B_{\mathrm{eff}}=\min(B,N_L-1)
$$

である。既定の要求帯域幅は $B=48$ である。

残差平均のHAC標準誤差は

$$
SE_{NW}(\bar e)
=\sqrt{\frac{\max(\widehat\Omega_{NW},0)}{N_L}}
$$

であり、HAC t統計量は

$$
t_{NW}
=\frac{\bar e}{SE_{NW}(\bar e)}
$$

である。両側p値は標準正規分布を用いて

$$
p_{NW}
=2\left[1-\Phi\left(|t_{NW}|\right)\right]
$$

とする。

### 13.11 最適窓の選択

候補窓 $E_L$ は、次の辞書式優先順位で昇順に並べる。

1. RMSE
2. MAE
3. lookback年数 $L$

したがって、最適窓は

$$
L^*
=\mathrm{arg\,min}_{L}
\left(
\mathrm{RMSE}(E_L),
\mathrm{MAE}(E_L),
L
\right)
$$

である。これは、原則としてRMSE最小の窓を選び、RMSEが同値の場合はMAEが小さい窓を選び、さらに同値の場合はより短い学習窓を選ぶことを意味する。

この選択では評価年度 $T$ を係数推定に含めないため、窓選択時点のルックアヘッド・バイアスを回避する。

### 13.12 最新年度を含む再推定

最適lookback年数 $L^*$ が決定した後、将来係数の推定では評価に使用した最新完全年度 $T$ まで学習窓を1年進める。再推定年度集合は

$$
E_{\mathrm{current}}
=\{T-L^*+1,\ldots,T\}
$$

である。

例えば、評価時点で $E_3=\{2021,2022,2023\}$ を用いてFY2024を評価し、$L^*=3$ が選ばれた場合、将来係数の再推定には

$$
E_{\mathrm{current}}=\{2022,2023,2024\}
$$

を使用する。

将来適用係数は

$$
D^{\mathrm{current}}_{m,h,g}
=D_{E_{\mathrm{current}},m,h,g}
$$

である。この処理により、窓長は過去のOOS成績で選択しつつ、係数値自体は利用可能な最新年度を含むデータで更新される。

### 13.13 将来カレンダーへの係数付与

最新完全年度を $T$、将来生成年数を $H$ とすると、将来テーブルの年度範囲は

$$
T+1,T+2,\ldots,T+H
$$

である。各将来年度について、年度開始日から年度終了日までの日次カレンダーを生成し、各日に48コマを直積する。

将来日 $d$ とコマ $h$ の受渡日時は

$$
\mathrm{DeliveryDateTime}(d,h)
=d+30(h-1)\text{ minutes}
$$

である。

将来日についても祝日判定、日種別判定、thetaグループ変換を行い、キー

$$
(m_d,h,\theta(q_d))
$$

で最新再推定係数を結合する。将来の各日・各コマへ付与される係数は

$$
D^{\mathrm{future}}_{d,h}
=D^{\mathrm{current}}_{m_d,h,\theta(q_d)}
$$

である。

この将来テーブルは価格水準そのものではなく、月平均価格に掛け合わせる相対シェイプを提供する。任意の将来月平均価格シナリオ $M_{y,m}$ が別途与えられる場合、将来コマ価格は

$$
\widehat P^{\mathrm{future}}_{d,h}
=M_{y_d,m_d}\,
D^{\mathrm{current}}_{m_d,h,\theta(q_d)}
$$

として構成できる。

### 13.14 将来サマリーテーブル

将来詳細テーブルは、将来年度 $y$、月 $m$、コマ $h$、thetaグループ $g$ 単位で集約する。係数値は同じキー内で一定であるため、サマリー係数は

$$
D^{\mathrm{summary}}_{y,m,h,g}
=D^{\mathrm{current}}_{m,h,g}
$$

である。

将来観測件数は、該当する将来日数を $n_{y,m,g}$ とすると

$$
N^{\mathrm{future}}_{y,m,h,g}=n_{y,m,g}
$$

となる。各日について各コマは1行ずつ生成されるため、固定した $h$ に対する行数と日数は一致する。

一方、`source_observation_count` は将来日数ではなく、係数推定に利用した履歴観測数

$$
N_{E_{\mathrm{current}},m,h,g}
$$

を表す。したがって、履歴データの厚みと将来カレンダー上の出現回数を区別して確認できる。

### 13.15 価格列ごとの独立推定

複数価格列が存在する場合、価格列を $c$、価格を $P_t^{(c)}$ と表す。すべての計算は価格列ごとに独立して行う。

$$
D_{E,m,h,g}^{(c)}
=\frac{\bar P_{E,m,h,g}^{(c)}}{\bar P_{E,m}^{(c)}}
$$

$$
L_c^*
=\mathrm{arg\,min}_{L}
\mathrm{RMSE}^{(c)}(E_L)
$$

したがって、異なるMarket Indexやエリア価格列が異なる最適lookback年数を選択することを許容する。最終出力では `price_column` により各系列を識別する。

### 13.16 数値不変性と処理順序

本ロジックでは、次の処理順序が数値定義の一部である。

1. 対象年度の全観測を抽出する。
2. 月平均を観測プールから計算する。
3. 月×コマ×thetaグループ別の条件付き平均を計算する。
4. 条件付き平均を月平均で除して係数を得る。
5. 評価年度の月平均に過去係数を掛けてOOS価格を再構築する。
6. RMSE、MAE、lookback年数の順に候補窓をランキングする。
7. 選択された窓長を維持して学習年度を最新年度までロールする。
8. 最新再推定係数を将来カレンダーへ決定論的に付与する。

特に、次の代替計算は現行ロジックと一般には同値でない。

- 年度別係数の単純平均
- 年度別月平均の単純平均
- 評価年度を含めた係数による窓選択
- 将来日数で再加重した係数の再推定
- RMSE計算前の月別または日別集約

したがって、リファクタリング時には、演算順序、`groupby`キー、欠損係数の除外条件、ソート順および浮動小数点演算の順序を保持する必要がある。

### 13.17 ロジック全体の要約式

本処理の中心は、次の3段階で要約できる。

**第1段階：過去年度集合 $E_L$ から相対シェイプを推定**

$$
D_{E_L,m,h,g}
=\frac{
\displaystyle
\frac{1}{N_{E_L,m,h,g}}
\sum_{t\in\mathcal{I}_{E_L,m,h,g}}P_t
}{
\displaystyle
\frac{1}{N_{E_L,m}}
\sum_{t\in\mathcal{I}_{E_L,m}}P_t
}.
$$

**第2段階：最新完全年度 $T$ でOOS検証し、最適窓長を選択**

$$
\widehat P_t^{(E_L)}
=\bar P_{T,m_t}D_{E_L,m_t,h_t,g_t},
$$

$$
L^*
=\mathrm{arg\,min}_{L}
\sqrt{
\frac{1}{N_L}
\sum_{t\in\mathcal{V}_{E_L,T}}
\left(P_t-\widehat P_t^{(E_L)}\right)^2
}.
$$

**第3段階：選択窓を最新年度まで進め、将来日付へ適用**

$$
E_{\mathrm{current}}
=\{T-L^*+1,\ldots,T\},
$$

$$
D^{\mathrm{future}}_{d,h}
=D_{E_{\mathrm{current}},m_d,h,\theta(q_d)}.
$$

この構成により、窓長選択は厳密なアウト・オブ・サンプル評価に基づき、将来係数は利用可能な最新データを反映し、将来日付への適用は月・祝休日区分・曜日グループ・48コマに基づく決定論的処理となる。
