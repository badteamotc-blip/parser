"""
Расширенные словари имён для Средней Азии (Казахстан, Узбекистан,
Киргизия, Таджикистан) + добор для Индонезии, Индии, Турции.

ЗАЧЕМ. Узбеки/таджики/киргизы/казахи часто пишут имя ОБЫЧНЫМИ русскими
буквами, без уникальных букв своего алфавита («Нурлан», «Айгерим»,
«Дилноза»). По символам их не отличить от русских — значит, ловим по
СЛОВАРЮ ИМЁН. Для cn/ru/jp/kr словари не нужны: их легко найти по
иероглифам/кане/хангылю/кириллице.

Имена даны и латиницей, и кириллицей (как реально пишут в Telegram).
~100 мужских + ~100 женских на страну (вместе с базовыми словарями в
chinese_detector.py). Все токены — нижний регистр.

ВНИМАНИЕ: часть имён общая для нескольких среднеазиатских стран и иногда
совпадает с русскими/арабскими (Тимур, Руслан, Камила). Это нормально —
поиск показывает КАНДИДАТОВ. Точную страну добивает нейросеть (nationalize).
"""

# ══════════════════════════════════════════════
#  🇰🇿 КАЗАХСТАН
# ══════════════════════════════════════════════
KAZAKH_EXTRA = {
    # муж — латиница
    "aldiyar", "alikhan", "ansar", "arsen", "beibarys", "bekzat", "didar",
    "dinmukhamed", "galymzhan", "ilyas", "iskander", "kasym", "kuat",
    "madi", "mukhtar", "nurassyl", "nurtas", "rauan", "sabyr", "sayan",
    "sultan", "tamerlan", "turar", "yerassyl", "yerkebulan", "zhasulan",
    "zhalgas", "abylaikhan", "bekzhan", "dauren", "magzhan", "miras",
    "nurali", "sapar", "shyngys", "ualikhan", "amanzhol", "beken",
    "dosjan", "elaman", "kuandyk", "sultanbek", "zhanibek", "nurzhan",
    # муж — кириллица
    "алдияр", "алихан", "ансар", "арсен", "бейбарыс", "бекзат", "дидар",
    "динмухамед", "галымжан", "ильяс", "искандер", "касым", "куат",
    "мади", "мухтар", "нурасыл", "нуртас", "рауан", "сабыр", "саян",
    "султан", "тамерлан", "турар", "ерасыл", "еркебулан", "жасулан",
    "жалгас", "абылайхан", "бекжан", "даурен", "магжан", "мирас",
    "нурали", "сапар", "шынгыс", "уалихан", "аманжол", "бекен",
    "досжан", "еламан", "куандык", "султанбек", "жанибек",
    # жен — латиница
    "adina", "aizere", "akmaral", "alua", "amina", "aruzhan", "assel",
    "ayala", "aziza", "dameli", "dilnaz", "gaukhar", "kamshat", "korlan",
    "laura", "meruert", "moldir", "nazerke", "perizat", "saltanat",
    "sezim", "sholpan", "togzhan", "ulzhan", "zhuldyz", "akerke", "aknur",
    "bayan", "dinara", "elnara", "gulnaz", "kunsulu", "makpal", "nazym",
    "symbat", "zhansulu", "aikorkem", "alima", "ainel", "tomiris",
    # жен — кириллица
    "адина", "айзере", "акмарал", "алуа", "амина", "аружан", "асель",
    "аяла", "азиза", "дамели", "дилназ", "гаухар", "камшат", "корлан",
    "лаура", "меруерт", "молдир", "назерке", "перизат", "салтанат",
    "сезим", "шолпан", "тогжан", "улжан", "жулдыз", "акерке", "акнур",
    "баян", "динара", "эльнара", "гульназ", "кунсулу", "макпал", "назым",
    "сымбат", "жансулу", "айкоркем", "алима", "аинель", "томирис",
}

# ══════════════════════════════════════════════
#  🇺🇿 УЗБЕКИСТАН
# ══════════════════════════════════════════════
UZBEK_EXTRA = {
    # муж — латиница
    "abror", "akbar", "akmaljon", "asad", "aybek", "baxtiyor", "bekzod",
    "davron", "diyorbek", "dostonbek", "elyor", "farrux", "firdavs",
    "husan", "ibrohim", "ikrom", "javlon", "jaloliddin", "kamron",
    "komron", "mirjalol", "nuriddin", "oybek", "ozod", "qodir",
    "sardorbek", "sherali", "sherzodbek", "sirojiddin", "tohir",
    "umarbek", "xushnud", "yusufbek", "zafarbek", "zohid", "anvarjon",
    "bobomurod", "davlat", "fayzulla", "husniddin", "jorabek", "sarvar",
    "shuhrat", "oybekjon", "muhammadali", "abdurashid",
    # муж — кириллица
    "аброр", "акбар", "акмалжон", "асад", "айбек", "бахтиёр", "бекзод",
    "даврон", "диёрбек", "достонбек", "элёр", "фаррух", "фирдавс",
    "хусан", "иброхим", "икром", "жавлон", "жалолиддин", "камрон",
    "комрон", "мирджалол", "нуриддин", "ойбек", "озод", "кодир",
    "сардорбек", "шерали", "шерзодбек", "сирожиддин", "тохир",
    "умарбек", "хушнуд", "юсуфбек", "зафарбек", "зохид", "анваржон",
    "бобомурод", "давлат", "файзулла", "хусниддин", "жорабек", "сарвар",
    "шухрат", "мухаммадали", "абдурашид",
    # жен — латиница
    "dilbar", "dilrabo", "durdona", "gulbahor", "gulchehra", "hilola",
    "laylo", "mahliyo", "marjona", "mavluda", "maxfuza", "maftuna",
    "mehribon", "munira", "nargiza", "nodira", "ozoda", "robiya",
    "sabohat", "saida", "sevinch", "shahnoza", "surayyo", "zarnigor",
    "zilola", "ziyoda", "gulasal", "kumush", "lobar", "malohat", "oygul",
    "sojida", "yulduz", "dilfuza", "nilufar", "barno", "iroda", "shoira",
    # жен — кириллица
    "дилбар", "дилрабо", "дурдона", "гулбахор", "гулчехра", "хилола",
    "лайло", "махлиё", "маржона", "мавлуда", "махфуза", "мафтуна",
    "мехрибон", "мунира", "наргиза", "нодира", "озода", "робия",
    "сабохат", "саида", "севинч", "шахноза", "сурайё", "зарнигор",
    "зилола", "зиёда", "гуласал", "кумуш", "лобар", "малохат", "ойгул",
    "сожида", "юлдуз", "дилфуза", "нилуфар", "шоира",
}

# ══════════════════════════════════════════════
#  🇰🇬 КИРГИЗИЯ
# ══════════════════════════════════════════════
KYRGYZ_EXTRA = {
    # муж — латиница
    "aibol", "atabek", "beksultan", "bektur", "chyngyzbek", "elaman",
    "emir", "ilim", "iskender", "izat", "kuban", "kylych", "marsel",
    "medet", "melis", "nuradil", "nurislam", "omurbek", "sanjarbek",
    "syrgak", "taalai", "tynchtyk", "ulanbek", "zhenish", "zhyrgalbek",
    "adil", "aman", "baiel", "dastanbek", "erkin", "myrzabek", "niyazbek",
    "turatbek", "akylbek", "nurlanbek", "samatbek", "bakytbek",
    # муж — кириллица
    "айбол", "атабек", "бексултан", "бектур", "чынгызбек", "эламан",
    "эмир", "илим", "искендер", "изат", "кубан", "кылыч", "марсель",
    "медет", "мелис", "нурадил", "нурислам", "омурбек", "санжарбек",
    "сыргак", "таалай", "тынчтык", "уланбек", "жениш", "жыргалбек",
    "адиль", "аман", "баель", "дастанбек", "эркин", "мырзабек",
    "ниязбек", "туратбек", "нурланбек", "саматбек", "бакытбек",
    # жен — латиница
    "aiturgan", "aijamal", "aida", "ainura", "aichurok", "albina",
    "baktygul", "elnura", "gulnur", "jazgul", "kanyshai", "kymbat",
    "meerim", "nazira", "nuraida", "saikal", "salima", "tursunai",
    "umut", "venera", "zhanyl", "zhibek", "aiperim", "aikanysh",
    "ainagul", "darikha", "elmira", "gulzada", "jamilya", "kanykei",
    "mahabat", "nurzat", "saadat", "ulpan", "begaiym", "cholpon",
    # жен — кириллица
    "айтурган", "айжамал", "аида", "айнура", "айчурок", "альбина",
    "бактыгуль", "элнура", "гульнур", "жазгуль", "канышай", "кымбат",
    "мээрим", "назира", "нураида", "сайкал", "салима", "турсунai",
    "турсунай", "умут", "венера", "жаныл", "жибек", "айперим",
    "айканыш", "айнагуль", "дарика", "эльмира", "гульзада", "жамиля",
    "каныкей", "махабат", "нурзат", "саадат", "улпан", "бегайым",
}

# ══════════════════════════════════════════════
#  🇹🇯 ТАДЖИКИСТАН
# ══════════════════════════════════════════════
TAJIK_EXTRA = {
    # муж — латиница
    "abdurahim", "akbarali", "alijon", "amriddin", "asadullo", "bahodur",
    "dilovar", "faridun", "firuz", "fozil", "hakim", "idibek", "iskandar",
    "jovid", "karim", "mahmadali", "manuchehr", "mehrubon", "mirali",
    "muhriddin", "najmiddin", "orzu", "ramazon", "rajab", "safarmurod",
    "sino", "sohibnazar", "talbak", "ubaidullo", "umedjon", "vali",
    "zafarjon", "zoir", "bobojon", "davlatsho", "gulom", "jamoliddin",
    "shariff", "khusrav", "dilshod", "farrukhsho",
    # муж — кириллица
    "абдурахим", "акбарали", "алиджон", "амриддин", "асадулло", "баходур",
    "диловар", "фаридун", "фируз", "фозил", "хаким", "идибек", "искандар",
    "джовид", "карим", "махмадали", "манучехр", "мехрубон", "мирали",
    "мухриддин", "наджмиддин", "орзу", "рамазон", "раджаб", "сафармурод",
    "сино", "сохибназар", "талбак", "убайдулло", "умедджон", "вали",
    "зафарджон", "зоир", "бободжон", "давлатшо", "гулом", "джамолиддин",
    "хусрав", "фаррухшо",
    # жен — латиница
    "anisa", "bahor", "dilafruz", "gulru", "gulshan", "jamila",
    "khursheda", "komila", "mahina", "mehri", "mehrigul", "nargis",
    "nasrin", "oisha", "parisa", "ramziya", "sabrina", "sadbarg",
    "saodat", "shabnam", "shukrona", "surayo", "takhmina", "zarrina",
    "zuhro", "husnoro", "jonona", "nozanin", "parvona", "ruziya",
    "dilnoza", "gulnoza", "manija", "nigina", "sitora", "shahnoza",
    # жен — кириллица
    "аниса", "бахор", "дилафруз", "гулру", "гулшан", "джамила",
    "хуршеда", "комила", "махина", "мехри", "мехригуль", "наргис",
    "насрин", "оиша", "париса", "рамзия", "сабрина", "садбарг",
    "саодат", "шабнам", "шукрона", "сурайо", "тахмина", "заррина",
    "зухро", "хуснаро", "джонона", "нозанин", "парвона", "рузия",
    "дилноза", "гулноза", "манижа", "нигина", "ситора",
}

# ══════════════════════════════════════════════
#  Добор к существующим словарям (id, in, tr) — латиница
# ══════════════════════════════════════════════
INDONESIAN_EXTRA = {
    # муж
    "abdul", "adit", "aldi", "alif", "andre", "anto", "ardi", "bagas",
    "candra", "didi", "fahmi", "ferdi", "gani", "hari", "hasan",
    "kiki", "lutfi", "rama", "randi", "rendi", "rian", "ridho", "rio",
    "riski", "rofiq", "samsul", "septian", "sigit", "slamet", "taufik",
    "tegar", "vino", "wawan", "yanto", "zaki", "zidan",
    # жен
    "amel", "anisa", "aulia", "cahaya", "desi", "dinda", "erika", "fitria",
    "kirana", "lina", "marni", "nadia", "nabila", "novita", "ratih",
    "sasa", "selvi", "tari", "tika", "winda", "zahra", "anggun", "bella",
    "cantika", "fani",
}

INDIAN_EXTRA = {
    # муж
    "aman", "ashwin", "bharat", "chirag", "darshan", "dev", "dhruv",
    "harish", "imran", "jay", "keshav", "lakshman", "mukesh", "nakul",
    "om", "parth", "raghav", "rishabh", "sahil", "siddharth", "tushar",
    "uday", "ved", "yuvraj", "abhinav", "akhil", "balaji", "chandan",
    # жен
    "aarti", "bhavna", "charu", "diksha", "ekta", "geeta", "hema", "ira",
    "juhi", "kajal", "lata", "mansi", "naina", "ojas", "pari", "rani",
    "sakshi", "tara", "uma", "vidya", "anita", "bina", "chitra", "esha",
    "gauri", "heena", "indu", "jaya", "kavya", "leela", "neelam",
}

TURKISH_EXTRA = {
    # муж
    "berat", "doruk", "ege", "emir", "kuzey", "mert", "poyraz",
    "sarp", "tunahan", "yaman", "atlas", "cinar", "efe", "kayra",
    # жен
    "asya", "azra", "beren", "defne", "ela", "eylul", "ilayda", "lara",
    "nehir", "nisanur", "oyku", "sila", "su", "zehra", "zumra",
    "asli", "aysu", "betul", "duru", "feyza", "hira", "miray",
}
