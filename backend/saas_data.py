"""Versioned, original Japanese demonstration records; no real advertisements or CVs."""

import hashlib
import random
import re
from datetime import date, timedelta
from functools import lru_cache

from backend.saas_models import (
    Area,
    Company,
    Credential,
    Dataset,
    Employment,
    Evaluation,
    Job,
    PersonRecord,
    Point,
    Schedule,
    Site,
)
from backend.saas_routing import NETWORK_VERSION, load_network

DATASET_REVISION = 2
DATASET_VERSION = f"saas-jp-original-20261006-v{DATASET_REVISION}"
TENANT_IDS = ("demo",) + tuple(f"demo{i:02}" for i in range(2, 11))
AS_OF_DATE = date(2026, 10, 6)
AREA_LABELS = {
    "大宮": "埼玉県さいたま市大宮区錦町周辺",
    "浦和": "埼玉県さいたま市浦和区高砂一丁目周辺",
    "川口": "埼玉県川口市川口一丁目周辺",
    "赤羽": "東京都北区赤羽一丁目周辺",
    "上野": "東京都台東区上野七丁目周辺",
    "秋葉原": "東京都千代田区外神田一丁目周辺",
    "神田": "東京都千代田区鍛冶町二丁目周辺",
    "東京": "東京都千代田区丸の内一丁目周辺",
    "品川": "東京都港区高輪三丁目周辺",
    "蒲田": "東京都大田区蒲田五丁目周辺",
    "川崎": "神奈川県川崎市川崎区駅前本町周辺",
    "鶴見": "神奈川県横浜市鶴見区鶴見中央一丁目周辺",
    "横浜": "神奈川県横浜市西区高島二丁目周辺",
    "錦糸町": "東京都墨田区江東橋三丁目周辺",
    "新小岩": "東京都葛飾区新小岩一丁目周辺",
    "市川": "千葉県市川市市川一丁目周辺",
    "西船橋": "千葉県船橋市西船四丁目周辺",
    "船橋": "千葉県船橋市本町七丁目周辺",
}

# Field conventions follow public Hello Work guidance; every description below is original.
OCCUPATIONS = (
    (
        "一般事務",
        "事務受託",
        "星みち事務サービス",
        1650,
        ("表計算", "文書作成", "電話応対"),
        (),
        ("日商簿記3級",),
        "申請書の受付、入力内容の突合、部署別の進捗表更新を担当します。",
        "差戻し理由を記録し、判断が必要な申請は担当社員に確認します。",
    ),
    (
        "経理補助",
        "経理業務受託",
        "あおば帳簿サポート",
        1850,
        ("仕訳照合", "表計算", "支払管理"),
        (),
        ("日商簿記2級",),
        "請求書と発注記録を照合し、支払予定一覧と月次集計の下資料を作成します。",
        "承認処理や振込操作は社員が行い、担当者は相違点の整理までを受け持ちます。",
    ),
    (
        "ITヘルプデスク",
        "情報サービス",
        "みなも情報支援",
        2100,
        ("端末設定", "障害切分け", "問合せ記録"),
        (),
        ("ITパスポート",),
        "社内端末の初期設定と一次問合せ対応を行い、対応履歴をチケットに残します。",
        "権限変更は承認後に実施し、未解決案件は再現手順を添えて専門担当へ引き継ぎます。",
    ),
    (
        "ソフトウェアテスト",
        "ソフトウェア品質支援",
        "ことの葉検証工房",
        2200,
        ("テスト実行", "不具合記録", "SQL基礎"),
        (),
        ("基本情報技術者",),
        "業務画面のテスト項目を実行し、期待結果との差を画像と操作手順で記録します。",
        "修正後の再確認と周辺機能の確認を分け、実施状況を日次で共有します。",
    ),
    (
        "倉庫入出庫",
        "物流",
        "あかつき物流企画",
        1650,
        ("在庫照合", "検品", "安全確認"),
        (),
        (),
        "入荷品の数量確認、保管棚への補充、出荷指示に沿った商品集めを担当します。",
        "台車を使い、重量物は複数名で扱います。棚番と品番を読み合わせて誤出荷を防ぎます。",
    ),
    (
        "フォークリフト作業",
        "物流",
        "こもれび配送基盤",
        1850,
        ("荷役操作", "在庫照合", "安全確認"),
        ("フォークリフト運転技能講習",),
        (),
        "リーチ式車両でパレットの入出庫を行い、作業前点検と棚卸しを担当します。",
        "歩行者区画との境界で一旦停止し、荷姿に不安がある場合は積替えを相談します。",
    ),
    (
        "検品包装",
        "食品製造",
        "なぎさ食品ラボ",
        1600,
        ("外観検査", "包装", "衛生管理"),
        (),
        ("食品衛生責任者",),
        "常温食品の包装状態と印字を確認し、規格別の箱詰めと検査記録を行います。",
        "判断が分かれる品は良品に混ぜず、専用トレーで班長の確認を受けます。",
    ),
    (
        "製造組立",
        "精密機器製造",
        "はるか部品工房",
        1750,
        ("工具操作", "図面確認", "寸法測定"),
        (),
        (),
        "作業標準に沿って小型機器を組み立て、締付け値と動作確認結果を記録します。",
        "工程変更時は試作品を確認してから量産に入り、不良の再発条件を班内で共有します。",
    ),
    (
        "設備保全補助",
        "施設保守",
        "つむぎ設備サービス",
        2300,
        ("電気点検", "安全確認", "点検記録"),
        ("第二種電気工事士",),
        (),
        "施設内の分電盤と照明設備を点検し、承認された小規模交換作業を担当します。",
        "停電範囲の確認と施錠管理を行い、高所作業は専任者と組んで実施します。",
    ),
    (
        "ホテルフロント",
        "宿泊",
        "月あかり宿泊企画",
        1800,
        ("受付案内", "予約照合", "引継ぎ記録"),
        (),
        (),
        "到着予定の照合、チェックイン案内、備品貸出しと会計明細の確認を行います。",
        "料金変更や返金は責任者が判断します。未完了の依頼を交代時に読み合わせます。",
    ),
    (
        "調理補助",
        "給食",
        "ひだまり食堂企画",
        1600,
        ("衛生管理", "盛付け", "食数管理"),
        (),
        ("調理師",),
        "社員食堂で下処理、盛付け、食器の洗浄を担当し、食数と残数を記録します。",
        "アレルゲン別の器具を分け、提供前の確認は調理担当者と二人で行います。",
    ),
    (
        "介護職",
        "介護サービス",
        "よりそいケア企画",
        1950,
        ("生活支援", "介護記録", "申し送り"),
        ("介護職員初任者研修",),
        ("介護福祉士",),
        "通所施設で食事・移動・更衣を支援し、利用時の様子を記録します。",
        "個別の支援計画を確認し、状態の変化は自己判断せず看護担当者へ報告します。",
    ),
    (
        "医療事務",
        "医療事務受託",
        "しおり窓口支援",
        1750,
        ("受付案内", "請求入力", "個人情報管理"),
        (),
        ("医療事務技能審査",),
        "受付番号の案内、保険情報の確認、診療後の会計入力と書類整理を担当します。",
        "算定上の疑義は管理者へ確認し、患者への医学的な説明は医療職へ引き継ぎます。",
    ),
    (
        "コールセンター",
        "顧客支援",
        "おといろ応対サービス",
        1750,
        ("問合せ応対", "要約入力", "手順確認"),
        (),
        (),
        "配送予定や手続きに関する受電に対応し、確認事項と回答を記録します。",
        "台本だけで解決しない相談は担当窓口へつなぎ、折返し期限を管理します。",
    ),
    (
        "販売接客",
        "生活用品小売",
        "くらしの輪商会",
        1650,
        ("商品案内", "レジ操作", "在庫照合"),
        (),
        (),
        "生活用品売場で商品案内、補充、会計と閉店時の売場整理を担当します。",
        "欠品時は入荷予定を確認してから案内し、返品は店内の承認手順に従います。",
    ),
    (
        "営業事務",
        "卸売",
        "あさつゆ資材商会",
        1850,
        ("受発注", "納期調整", "表計算"),
        (),
        (),
        "法人顧客からの注文を登録し、在庫確認、納期連絡、見積書の下書きを担当します。",
        "受注条件の変更は履歴を残し、単価や特別条件は営業担当者の承認を受けます。",
    ),
    (
        "配送助手",
        "地域配送",
        "かけはし配送企画",
        1700,
        ("積荷照合", "安全確認", "受渡し記録"),
        (),
        (),
        "運転担当者に同行し、積荷の照合、納品先での台車搬送、受領確認を担当します。",
        "車両の運転は業務に含みません。通路の確保を確認してから搬入を始めます。",
    ),
    (
        "法人営業支援",
        "業務支援",
        "まどか事業支援",
        2250,
        ("提案資料", "顧客記録", "日程調整"),
        (),
        (),
        "既存顧客への定期連絡、利用状況の整理、提案資料と面談記録の作成を行います。",
        "契約金額や納期の確約は責任者が行い、担当者は課題の整理と社内調整を担います。",
    ),
    (
        "CADオペレーター",
        "設計支援",
        "すみれ設計支援",
        2350,
        ("CAD作図", "図面照合", "版管理"),
        (),
        ("CAD利用技術者2級",),
        "指示図に基づいて設備配置図を修正し、寸法・注記の照合と図面台帳更新を行います。",
        "設計判断は有資格の担当者が行います。修正前後の差分を提出して確認を受けます。",
    ),
    (
        "清掃管理補助",
        "建物管理",
        "そよかぜ環境企画",
        1600,
        ("清掃手順", "安全確認", "点検記録"),
        (),
        ("ビルクリーニング技能士",),
        "共用部の定期清掃、消耗品の補充、完了点検と資材残数の確認を担当します。",
        "濡れた床は区画を表示し、洗剤は指定の濃度と組合せを守って使用します。",
    ),
)
SURNAMES = (
    "青木",
    "石井",
    "上田",
    "遠藤",
    "大野",
    "加藤",
    "川村",
    "木村",
    "久保",
    "小林",
    "斎藤",
    "坂本",
    "佐々木",
    "清水",
    "鈴木",
    "高橋",
    "田中",
    "千葉",
    "津田",
    "寺田",
    "中川",
    "中村",
    "西田",
    "野口",
    "橋本",
    "林",
    "平井",
    "藤井",
    "藤田",
    "古川",
    "松井",
    "松本",
    "三浦",
    "宮本",
    "村田",
    "森",
    "山口",
    "山田",
    "吉田",
    "渡辺",
)
GIVEN_NAMES = (
    "葵",
    "明",
    "泉",
    "伊織",
    "海",
    "薫",
    "奏",
    "恵",
    "光",
    "渚",
    "晴",
    "遥",
    "歩",
    "望",
    "真琴",
    "瑞希",
    "悠",
    "優",
    "柚",
    "涼",
    "律",
    "凛",
    "蓮",
    "楓",
    "翼",
    "環",
    "千尋",
    "直",
    "響",
    "碧",
    "晶",
    "光希",
    "智",
    "仁",
    "和",
    "翠",
    "実",
    "悠希",
    "夏",
    "春",
)


def dataset_provenance() -> dict:
    return {
        "version": DATASET_VERSION,
        "as_of_date": AS_OF_DATE.isoformat(),
        "tenant_ids": list(TENANT_IDS),
        "tenant_id_policy": (
            "Any nonblank string; tenant_ids enumerates default demo tenants, not an allowlist."
        ),
        "default_counts": {"tenants": 10, "jobs_per_tenant": 1000, "people_per_tenant": 1000},
        "original_synthetic_text": True,
        "source_personal_data_or_job_ads": False,
        "sampling": (
            "Versioned tenant/domain/record seeds. Jobs and people independently shuffle "
            "20 occupation strata (50 each at full size). Unique full names are sampled "
            "without replacement independently of profiles. No index-derived attributes."
        ),
        "intentional_dependencies": (
            "Occupation informs skills, qualifications, pay and work history; experience "
            "informs skill levels. Homes favor one of the occupation's three workplace "
            "catchments, not the job at the same index. Names do not affect matching."
        ),
        "date_intervals": "[start_date, end_date); availability end dates also exclusive",
        "geography": (
            "固定の駅代表点を町丁目周辺の概略位置として表示。町丁目境界・建物の入居情報ではない。"
        ),
        "locations": "架空の拠点・居所を公開道路ノードに接続。実在建物への入居や居住を示さない。",
        "pay": "時給は架空の募集条件。最低1,600円以上、深夜・時間外割増は別途。",
        "protected_attributes": "年齢・性別・国籍・健康状態等の適性評価用属性は生成しない。",
        "geographic_source_version": NETWORK_VERSION,
        "geographic_attribution": "© OpenStreetMap contributors",
        "geographic_license": "ODbL-1.0",
        "geographic_license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
    }


@lru_cache(maxsize=1)
def _locations() -> tuple:
    network = load_network()
    locations = []
    rail_points = {network.nodes[node] for edge in network.rail_edges for node in edge["nodes"]}
    for name, label in AREA_LABELS.items():
        station = network.station_by_name[name]
        lat, lon = station["point"]
        area = Area(id=station["id"] + "-area", label=label, point=Point(lat=lat, lon=lon))
        candidates = [
            node
            for node, seconds in sorted(network.station_walks(station["id"]).items())
            if 50 <= seconds <= 600
            and node in network.car
            and node in network.reverse_car
            and network.nodes[node] not in rail_points
        ]
        accepted = 0
        for node in candidates:
            options = network.station_options(node)
            if len(options) < 2:
                continue
            locations.append((area, node, tuple(options)))
            accepted += 1
            if accepted == 24:
                break
    if len(locations) < 60:
        raise ValueError("Insufficient connected multi-station locations in the pinned extract")
    return tuple(locations)


def _rng(tenant_id: str, domain: str) -> random.Random:
    seed = hashlib.sha256(f"{DATASET_VERSION}\0{tenant_id}\0{domain}".encode()).digest()
    return random.Random(int.from_bytes(seed, "big"))


def _occupations(tenant_id: str, domain: str) -> list[int]:
    # Fixed-size independent strata keep small fixtures a stable prefix of the full dataset.
    indices = [index for index in range(len(OCCUPATIONS)) for _ in range(50)]
    _rng(tenant_id, domain).shuffle(indices)
    return indices


def _schedule(rng: random.Random, *, availability: bool = False, night: bool = False) -> Schedule:
    weekdays = ([0, 1, 2, 3, 4], [1, 2, 3, 4, 5], [0, 2, 4], [2, 3, 4, 5, 6])
    if availability:
        days = rng.choices((*weekdays, list(range(7))), weights=(50, 10, 10, 10, 20))[0]
        windows = ((420, 1200), (480, 1140), (540, 1080), (540, 960), (600, 1200))
        weights = (25, 20, 30, 15, 10)
    else:
        days = rng.choices(weekdays, weights=(55, 15, 15, 15))[0]
        windows = ((540, 1080), (480, 1020), (540, 960), (600, 1140), (780, 1320))
        weights = (45, 20, 10, 20, 5)
    start, end = (
        (1200, 1800 if availability else 1740)
        if night and rng.random() < 0.15
        else rng.choices(windows, weights=weights)[0]
    )
    return Schedule(
        weekdays=list(days),
        start_minute=start,
        end_minute=end,
        break_minutes=60,
    )


def _history(occupation: tuple, rng: random.Random) -> list[Employment]:
    if rng.random() < 0.15:
        return []
    title, industry, _, _, skills, *_ = occupation
    end = AS_OF_DATE - timedelta(days=rng.choice((6, 30, 120, 360, 720, 1260)))
    records = []
    for number in range(rng.randint(1, 3)):
        months = rng.choice((8, 14, 24, 36))
        start = end - timedelta(days=months * 30)
        evaluations = []
        if rng.random() < 0.75:
            observations = (
                f"{skills[0]}の手順を確認して作業を進めた。例外案件は記録を残して相談していた。",
                f"{skills[1]}で見つけた相違を一覧に整理し、担当者が確認しやすい順に報告した。",
                "交代時に未完了の作業と確認待ちの案件を分けて伝えていた。",
                "繁忙日に作業順を見直した。新しい手順の定着には追加の練習が必要だった。",
            )
            dimensions = ("attendance", "quality", "collaboration", "improvement")
            for offset, dimension_index in enumerate(rng.sample(range(4), rng.randint(1, 4))):
                dimension = dimensions[dimension_index]
                observation = observations[dimension_index]
                if dimension == "attendance":
                    observation = (
                        "予定変更が必要な日は事前に連絡し、引継ぎ相手と作業期限を確認していた。"
                        "勤務記録の修正は当月中に申請した。"
                    )
                evaluations.append(
                    Evaluation(
                        recorded_at=end - timedelta(days=15 + offset * 20),
                        observation=observation,
                        dimension=dimension,
                        score=None
                        if rng.random() < 0.15
                        else rng.choices((2, 3, 4, 5), (10, 30, 45, 15))[0],
                        source=f"架空の就業先担当者による第{number + 1}期業務振り返り",
                    )
                )
        records.append(
            Employment(
                employer=f"架空・{industry}事業所{rng.randint(1, 29):02}-{number + 1}",
                occupation=title,
                start_date=start,
                end_date=end,
                duties=f"{occupation[-2]}週ごとの作業量を記録し、{skills[0]}と{skills[1]}を担当した。",
                evaluations=evaluations,
            )
        )
        end = start - timedelta(days=rng.randint(15, 90))
    return list(reversed(records))


def generate_tenant(tenant_id: str, *, count: int = 1000) -> Dataset:
    """Generate records for a nonblank tenant ID; does not create or authorize an account."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a nonblank string")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
        raise ValueError("count must be an integer from 1 to 1000")
    contact_label = (
        tenant_id.lower()
        if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", tenant_id)
        else "tenant-" + hashlib.sha256(tenant_id.encode()).hexdigest()[:20]
    )
    contact_domain = f"{contact_label}.example.invalid"
    network = load_network()
    locations = _locations()
    job_occupations = _occupations(tenant_id, "job-occupations")
    person_occupations = _occupations(tenant_id, "person-occupations")
    names = _rng(tenant_id, "names").sample(
        [(surname, given) for surname in SURNAMES for given in GIVEN_NAMES],
        1000,
    )
    night_occupations = {
        "倉庫入出庫",
        "フォークリフト作業",
        "検品包装",
        "製造組立",
        "ホテルフロント",
    }
    companies, sites, jobs, people = [], [], [], []
    for company_index, occupation in enumerate(OCCUPATIONS):
        company_id = f"{tenant_id}-company-v{DATASET_REVISION}-{company_index + 1:02}"
        companies.append(
            Company(
                id=company_id,
                name=f"株式会社{occupation[2]}（架空）",
                industry=occupation[1],
                description=(
                    f"{occupation[1]}を想定した検証用の架空企業です。"
                    "本社で受注・教育を担い、支店と作業拠点で日々の業務を行う設定です。"
                    "記載の募集・採用・連絡先はいずれも実在しません。"
                ),
                contact_email=f"recruit.{company_index + 1:02}@{contact_domain}",
            )
        )
        for site_index, (kind, label) in enumerate(
            (
                ("headquarters", "本社"),
                ("branch", "支店"),
                ("worksite", "業務センター"),
            )
        ):
            rng = _rng(tenant_id, f"site:{company_index}:{site_index}")
            area, node, options = rng.choice(locations)
            lat, lon = network.nodes[node]
            sites.append(
                Site(
                    id=f"{company_id}-site-{site_index + 1}",
                    company_id=company_id,
                    name=f"{occupation[2]} {options[0].name}{label}（架空）",
                    kind=kind,
                    address=(
                        f"{area.label}・架空街区{company_index + 1}番{site_index + 1}号"
                        "（実在住所ではない）"
                    ),
                    area=area.model_copy(deep=True),
                    point=Point(lat=lat, lon=lon),
                    stations=[option.model_copy() for option in options],
                    parking=rng.random() < 0.65,
                    accessibility=(
                        "架空拠点は段差のない出入口とエレベーターを備える設定。"
                        "駅のバリアフリー経路は未検証のため、必要な設備は個別確認します。"
                    ),
                )
            )
    for index in range(count):
        rng = _rng(tenant_id, f"job:{index}")
        occupation_index = job_occupations[index]
        occupation = OCCUPATIONS[occupation_index]
        title, _, _, pay, skills, required, preferred, duties, caution = occupation
        company = companies[occupation_index]
        site = rng.choice(sites[occupation_index * 3 : occupation_index * 3 + 3])
        start = date(2026, 11, 2) + timedelta(weeks=rng.randrange(6))
        end = start + timedelta(days=rng.choice((90, 180, 270)))
        schedule = _schedule(rng, night=title in night_occupations)
        required_skills = {skill: rng.choices((1, 2, 3), (30, 50, 20))[0] for skill in skills}
        required_qualifications = list(required)
        if preferred and rng.random() < 0.12:
            required_qualifications.append(rng.choice(preferred))
        experience = rng.choices((0, 6, 12, 24), (40, 30, 20, 10))[0]
        placement = rng.random() < 0.35
        pay_adjustment = rng.choice((0, 50, 100, 150, 200, 300))
        job = Job(
            id=f"{tenant_id}-job-v{DATASET_REVISION}-{index + 1:04}",
            company_id=company.id,
            company_name=company.name,
            site_id=site.id,
            site=site,
            title=f"{title}／{site.stations[0].name}・募集番号{index + 1:04}",
            occupation=title,
            duties=(
                f"{duties}{caution}配属先は{rng.randint(2, 8)}名のチームです。"
                f"初日は{rng.randint(1, 3)}時間の手順説明と見学を行い、"
                f"募集番号{index + 1:04}の担当表に沿って作業範囲を確認します。"
            ),
            business_type="placement" if placement else "dispatch",
            employment_type="有期契約社員" if placement else "有期雇用派遣",
            headcount=rng.choices((1, 2, 3), (55, 30, 15))[0],
            start_date=start,
            end_date=end,
            schedule=schedule,
            hourly_pay_min=pay + pay_adjustment,
            hourly_pay_max=pay + pay_adjustment + rng.choice((150, 200, 300)),
            required_skills=required_skills,
            required_qualifications=required_qualifications,
            preferred_qualifications=[
                value for value in preferred if value not in required_qualifications
            ],
            experience_months=experience,
            renewal=(
                "更新の可能性あり。業務量、契約満了時の進捗、勤務実績を双方で確認して判断。"
                "更新は2回まで、通算契約期間の上限は18か月。自動更新ではありません。"
            )
            if rng.random() < 0.75
            else "更新なし。記載期間の限定業務です。",
            duties_change_scope=f"変更範囲：{title}に付随する確認・記録業務。別職種への変更なし。",
            location_change_scope="変更範囲：同一拠点内。別拠点への異動なし。",
            overtime="原則なし。繁忙時は月5時間以内、事前相談のうえ別途割増支給。"
            if rng.random() < 0.5
            else "月5〜10時間程度を想定。事前承認制、法定の割増賃金を別途支給。",
            holidays="記載曜日以外は休み。祝日の扱いと希望休は契約前に提示する勤務表で確認。",
            allowances=(
                "交通費は月額30,000円を上限に規程支給。時間外・深夜・法定休日の割増は別途。"
                "自動車通勤は運転資格、保険、拠点駐車場の確認後に許可。"
            ),
            benefits=[
                "加入要件を満たす場合は社会保険・雇用保険",
                "労災保険",
                "法定の年次有給休暇",
                "業務手順研修",
                "就業場所は屋内禁煙",
            ],
            application=(
                "職歴と希望条件を確認後、業務説明と面談を1回予定。"
                "必要資格は原本等で確認します。実際の応募や外部への連絡は行わない検証用求人です。"
            ),
            status="closed" if rng.random() < 0.03 else "open",
        )
        jobs.append(job)
    for index in range(count):
        rng = _rng(tenant_id, f"person:{index}")
        occupation_index = person_occupations[index]
        occupation = OCCUPATIONS[occupation_index]
        title, _, _, pay, skills, required, preferred, *_ = occupation
        catchment = rng.choice(sites[occupation_index * 3 : occupation_index * 3 + 3]).area.id
        homes = (
            [location for location in locations if location[0].id == catchment]
            if rng.random() < 0.7
            else locations
        )
        home_area, node, options = rng.choice(homes)
        private_lat, private_lon = network.nodes[node]
        credentials = [
            Credential(name=name, verified=rng.random() < 0.93, valid_until=None)
            for name in dict.fromkeys((*required, *preferred))
            if rng.random() < (0.9 if name in required else 0.6)
        ]
        has_license = rng.random() < 0.75
        license_expired = rng.random() < 0.06
        license_verified = rng.random() < 0.95
        if has_license:
            credentials.append(
                Credential(
                    name="普通自動車第一種運転免許",
                    verified=license_verified,
                    valid_until=date(2026, 8, 31) if license_expired else date(2029, 8, 31),
                )
            )
        driving = has_license and not license_expired and license_verified
        history = _history(occupation, _rng(tenant_id, f"history:{index}"))
        person_skills = {
            skill: rng.choices((1, 2, 3, 4, 5), (3, 12, 35, 30, 20))[0]
            if history
            else rng.randint(1, 3)
            for skill in skills
        }
        if rng.random() < 0.1:
            person_skills.pop(rng.choice(skills))
        availability = _schedule(rng, availability=True, night=title in night_occupations)
        surname, given = names[index]
        related = [
            other[0]
            for other in OCCUPATIONS
            if other[0] != title and set(other[4]).intersection(skills)
        ]
        preferences = [title]
        if related and rng.random() < 0.6:
            preferences.append(rng.choice(related))
        people.append(
            PersonRecord(
                id=f"{tenant_id}-person-v{DATASET_REVISION}-{index + 1:04}",
                name=f"{surname} {given}（架空）",
                occupation=title,
                home_area=home_area.model_copy(deep=True),
                stations=[option.model_copy() for option in options],
                skills=person_skills,
                qualifications=credentials,
                employment_history=history,
                available_from=date(2026, 11, 2) + timedelta(weeks=rng.randrange(8))
                if rng.random() < 0.1
                else date(2026, 10, 15),
                available_until=rng.choice((date(2027, 2, 1), date(2027, 5, 1)))
                if rng.random() < 0.1
                else date(2028, 1, 1),
                availability=availability,
                desired_hourly_pay=pay
                + rng.choices((-200, -100, 0, 100, 300), (15, 25, 35, 20, 5))[0],
                preferred_occupations=preferences,
                commute_modes=rng.choices((["car"], ["transit", "car"], ["transit"]), (15, 45, 40))[
                    0
                ]
                if driving
                else ["transit"],
                driving_eligible=driving,
                max_commute_minutes=rng.choices(
                    (45, 60, 90, 120, 179, 180), (10, 30, 30, 20, 5, 5)
                )[0],
                matching_enabled=rng.random() < 0.98,
                private_location=Point(lat=private_lat, lon=private_lon),
                private_address=f"{home_area.label}・架空住区{index + 1}号（実在住所ではない）",
                contact_email=f"person.{index + 1:04}@{contact_domain}",
            )
        )
    return Dataset(
        tenant_id=tenant_id,
        version=DATASET_VERSION,
        companies=companies,
        sites=sites,
        jobs=jobs,
        people=people,
    )
