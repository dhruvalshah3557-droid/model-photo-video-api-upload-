#!/usr/bin/env python3
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main
from caption_generator import generate_caption, generate_hashtags, get_lang
from sheets_reader import SheetsReader


class RegionalLanguageTests(unittest.TestCase):
    def test_pakistan_uses_urdu_caption_hashtags_and_link_label(self):
        account = {
            "primary_language": "ur-PK",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Pakistan",
        }
        job = {"platform": "facebook", "account_id": "FB-PAK"}
        source = {
            "sku": "PK-1",
            "product_name": "Fancy Yellow Diamond Ring",
            "details": "Fancy Yellow Diamond Ring",
            "product_link": "https://www.colourdiam.com/product/PK-1",
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#Diamond",
        }

        caption = main.build_caption(job, source, account)

        self.assertIn("#پاکستان", caption)
        self.assertIn("پروڈکٹ دیکھیں:", caption)
        self.assertNotIn("View product:", caption)

    def test_hong_kong_short_name_uses_chinese(self):
        self.assertEqual(get_lang("Colour Diam HK"), "zh-hant")
        self.assertEqual(get_lang("colourdiamhk"), "zh-hant")

    def test_hong_kong_locale_uses_traditional_chinese(self):
        account = {
            "primary_language": "zh-HK",
            "fallback_language": "en-GB",
            "account_name": "Colourdiamhk",
        }
        job = {"platform": "facebook", "account_id": "FB-HK"}
        source = {
            "sku": "HK-1",
            "product_name": "Fancy Pink Diamond Ring",
            "details": "Fancy Pink Diamond Ring",
            "product_link": "https://www.colourdiam.com/product/HK-1",
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#Diamond",
        }

        caption = main.build_caption(job, source, account)

        self.assertIn("#香港", caption)
        self.assertIn("查看產品:", caption)

    def test_colour_diam_does_not_match_philippines_by_substring(self):
        self.assertEqual(get_lang("Colour Diam"), "en")
        self.assertEqual(get_lang("Colour Diam Philippines"), "tl")

    def test_configured_account_language_overrides_page_name_guess(self):
        product = {"title": "Pink Diamond", "description": "Rare natural stone"}
        caption = generate_caption(product, "Colour Diam Philippines", "en-GB")
        hashtags = generate_hashtags(product, "Colour Diam Philippines", "en-GB")
        self.assertNotIn("Gumawa ng pahayag", caption)
        self.assertIn("#FineJewelry", hashtags)

    def test_all_configured_regional_columns_are_mapped(self):
        expected = {
            "ar": ("arabic description", "arabic hashtag"),
            "vi": ("Vietnamese Description", "Vietnamese Hashtag"),
            "zh": ("Chinese Description", "Chinese Hashtag"),
            "sv": ("sweden description", "sweden hashtag"),
            "de": ("german description", "german hashtag"),
            "pl": ("polish description", "polish hahstag"),
            "da": ("danish description", "danish hashtag"),
            "fr": ("french description", "french hashtag"),
            "tr": ("turkish description", "turkish hashtag"),
            "it": ("italian description", "italian hashtag"),
            "es": ("spanish description", "spanish hashtag"),
            "ja": ("Japanese Description", "Japanese Hashtag"),
            "ko": ("Korean Description", "Korean Hashtag"),
            "ru": ("Russian Description", "Russian Hashtag"),
            "he": ("israli description", "israli hashtag"),
            "id": ("Indonesian description", "Indonesian hashtag"),
            "my": ("Burmese Description", "Burmese Hashtag"),
            "th": ("Thai Description", "Thai Hashtag"),
            "fil": ("Filipino Description", "Filipino Hashtag"),
            "lb": ("lebenesse description", "lebenesse hashtag"),
            "cs": ("vestslavic description", "vestslavic hashtag"),
        }
        for code, (caption_col, hashtag_col) in expected.items():
            self.assertEqual(SheetsReader.LANG_CAPTION_COLS[code], caption_col)
            self.assertEqual(SheetsReader.LANG_TAG_COLS[code], hashtag_col)

    def test_local_caption_wins_and_regional_english_fallback_is_blocked(self):
        source = {
            "lang_captions": {"it": "Descrizione italiana"},
            "lang_hashtags": {"it": "#gioielli"},
            "facebook_caption": "English Facebook caption",
            "instagram_caption": "English Instagram caption",
            "youtube_shorts_caption": "English YouTube caption",
            "hashtags": "#diamond",
            "product_link": "",
            "product_name": "Ring",
        }
        account = {
            "primary_language": "it-IT",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Italy",
        }
        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertEqual(
            caption,
            "Descrizione italiana\n\n#Diamanti #Gioielli #AltaGioielleria",
        )

        account["primary_language"] = "vi-VN"
        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertIn("#KimCương", caption)
        self.assertNotIn("English Instagram caption", caption)
        self.assertNotIn("#diamond", caption)

    def test_greece_generates_native_fallback_when_source_has_no_greek_column(self):
        source = {
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
            "product_name": "Fancy Yellow Diamond Ring",
        }
        account = {
            "primary_language": "el-GR",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Greece",
        }

        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertIn("#Διαμάντια", caption)
        self.assertIn("Δείτε το προϊόν:", caption)
        self.assertNotIn("#diamond", caption)

    def test_lebanon_uses_lebenesse_source_columns(self):
        row = {
            "lebenesse description": "وصف لبناني فاخر",
            "lebenesse hashtag": "#ألماس, #لبنان",
        }
        caption = SheetsReader._pick(
            row,
            SheetsReader.LANG_CAPTION_COLS["lb"],
            *SheetsReader.LANG_CAPTION_ALIASES["lb"],
        )
        hashtag = SheetsReader._pick(
            row,
            SheetsReader.LANG_TAG_COLS["lb"],
            *SheetsReader.LANG_TAG_ALIASES["lb"],
        )
        self.assertEqual(caption, "وصف لبناني فاخر")
        self.assertEqual(hashtag, "#ألماس, #لبنان")

    def test_czech_uses_vestslavic_source_columns(self):
        row = {
            "vestslavic description": "Popis v cestine",
            "vestslavic hashtag": "#Diamanty, #Sperky",
        }
        caption = SheetsReader._pick(
            row,
            SheetsReader.LANG_CAPTION_COLS["cs"],
            *SheetsReader.LANG_CAPTION_ALIASES["cs"],
        )
        hashtag = SheetsReader._pick(
            row,
            SheetsReader.LANG_TAG_COLS["cs"],
            *SheetsReader.LANG_TAG_ALIASES["cs"],
        )
        self.assertEqual(caption, "Popis v cestine")
        self.assertEqual(hashtag, "#Diamanty, #Sperky")

    def test_lebanon_generates_native_fallback_when_source_cell_is_empty(self):
        source = {
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
            "product_name": "Fancy Yellow Diamond Ring",
        }
        account = {
            "primary_language": "lb-LB",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Lebanon",
        }

        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertIn("#ألماس", caption)
        self.assertIn("عرض المنتج:", caption)
        self.assertNotIn("#diamond", caption)

    def test_czech_generates_native_fallback_when_source_cell_is_empty(self):
        source = {
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
            "product_name": "Fancy Yellow Diamond Ring",
        }
        account = {
            "primary_language": "cs-CZ",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Czech",
        }

        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("#Diamanty", caption)
        self.assertIn("Zobrazit produkt:", caption)
        self.assertNotIn("#diamond", caption)

    def test_turkey_uses_misnamed_greek_source_columns(self):
        row = {
            "greek description": "2.01 karat sari pirlanta yuzuk",
            "greek hashtag ": "#SariPirlanta, #LuksMucevher",
        }
        caption = SheetsReader._pick(
            row,
            SheetsReader.LANG_CAPTION_COLS["tr"],
            *SheetsReader.LANG_CAPTION_ALIASES["tr"],
        )
        hashtag = SheetsReader._pick(
            row,
            SheetsReader.LANG_TAG_COLS["tr"],
            *SheetsReader.LANG_TAG_ALIASES["tr"],
        )
        self.assertEqual(caption, "2.01 karat sari pirlanta yuzuk")
        self.assertEqual(hashtag, "#SariPirlanta, #LuksMucevher")

    def test_turkey_generates_native_fallback_when_source_cell_is_empty(self):
        source = {
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
            "product_name": "Fancy Yellow Diamond Ring",
        }
        account = {
            "primary_language": "tr-TR",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Turkey",
        }

        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("#Elmas", caption)
        self.assertIn("Ürünü görüntüle:", caption)
        self.assertNotIn("#diamond", caption)
        self.assertNotIn("Missing required tr regional caption", caption)

    def test_turkish_headers_ignore_trailing_spaces_and_case(self):
        row = {
            "Turkish Description": "Türkçe ürün açıklaması",
            "turkish hashtag ": "#DoğalElmas, #LüksMücevher",
        }
        caption = SheetsReader._pick(
            row, SheetsReader.LANG_CAPTION_COLS["tr"]
        )
        hashtag = SheetsReader._pick(
            row, SheetsReader.LANG_TAG_COLS["tr"]
        )
        self.assertEqual(caption, "Türkçe ürün açıklaması")
        self.assertEqual(hashtag, "#DoğalElmas, #LüksMücevher")

    def test_vietnam_source_import_headers_are_supported(self):
        row = {
            "vietnam description": "Mô tả tiếng Việt",
            "vietnam hashtag": "#KimCuong",
        }
        caption = SheetsReader._pick(
            row,
            SheetsReader.LANG_CAPTION_COLS["vi"],
            *SheetsReader.LANG_CAPTION_ALIASES["vi"],
        )
        hashtag = SheetsReader._pick(
            row,
            SheetsReader.LANG_TAG_COLS["vi"],
            *SheetsReader.LANG_TAG_ALIASES["vi"],
        )
        self.assertEqual(caption, "Mô tả tiếng Việt")
        self.assertEqual(hashtag, "#KimCuong")

    def test_vietnam_uses_guaranteed_native_hashtags(self):
        source = {
            "lang_captions": {"vi": "Mô tả tiếng Việt"},
            "lang_hashtags": {
                "vi": "KimCuongThienNhien, #ColourDiam, TrangSucCaoCap"
            },
            "hashtags": "#diamond",
            "product_link": "",
        }
        account = {
            "primary_language": "vi-VN",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Vietnam",
        }
        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertEqual(
            caption,
            "Mô tả tiếng Việt\n\n"
            "#KimCương #TrangSức #TrangSứcCaoCấp",
        )

    def test_existing_space_separated_hashtags_stay_valid(self):
        self.assertEqual(
            main._normalize_hashtags("#ColourDiam #KimCuong #GIA"),
            "#ColourDiam #KimCuong #GIA",
        )

    def test_regional_account_never_falls_back_to_english_hashtags(self):
        source = {
            "lang_captions": {"vi": "Mô tả tiếng Việt"},
            "lang_hashtags": {},
            "hashtags": "#diamond #jewelry",
            "product_link": "",
        }
        account = {
            "primary_language": "vi-VN",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Vietnam",
        }
        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertEqual(
            caption,
            "Mô tả tiếng Việt\n\n#KimCương #TrangSức #TrangSứcCaoCấp",
        )
        self.assertNotIn("#diamond", caption)

    def test_each_configured_region_has_native_fallback_hashtags(self):
        regional_codes = set(SheetsReader.LANG_TAG_COLS) - {"en"}
        self.assertTrue(regional_codes <= set(main._REGIONAL_FALLBACK_HASHTAGS))

    def test_product_link_label_is_localized(self):
        source = {
            "lang_captions": {"th": "คำบรรยายภาษาไทย"},
            "lang_hashtags": {"th": "#NaturalDiamond"},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
        }
        account = {
            "primary_language": "th-TH",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Bangkok",
        }
        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("ดูสินค้า: https://colourdiam.com/product/1", caption)
        self.assertNotIn("View product", caption)

    def test_kuwait_instagram_uses_clickable_bio_cta_not_caption_url(self):
        source = {
            "sku": "298",
            "lang_captions": {"ar": "وصف عربي فاخر"},
            "lang_hashtags": {"ar": "#ألماس"},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/productdetail/298",
        }
        account = {
            "primary_language": "ar-KW",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Kuwait",
        }
        caption = main.build_caption(
            {"platform": "instagram", "account_id": "IG-KUWAIT"},
            source,
            account,
        )
        self.assertIn("الرابط في السيرة الذاتية", caption)
        self.assertIn("رمز المنتج: 298", caption)
        self.assertNotIn("https://colourdiam.com/productdetail/298", caption)

        facebook_caption = main.build_caption(
            {"platform": "facebook", "account_id": "FB-KUWAIT"},
            source,
            account,
        )
        self.assertIn("https://colourdiam.com/productdetail/298", facebook_caption)

    def test_english_caption_uses_details_when_product_name_column_is_missing(self):
        source = {
            "sku": "309",
            "product_name": "0.16 Ct Fancy Deep Pink Marquise Diamond SI2",
            "details": "Sku - 309\nWeight - 0.16\nColour - Fancy Deep Pink",
            "product_link": "https://www.colourdiam.com/diamonddetails/Diamonds/Product/309",
            "facebook_caption": "",
            "instagram_caption": "",
            "hashtags": "",
            "lang_captions": {},
            "lang_hashtags": {},
        }
        account = {"primary_language": "en-GB", "account_name": "Colour Diam"}
        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("0.16 Ct Fancy Deep Pink", caption)
        self.assertFalse(main._caption_has_blank_product(caption))

    def test_blank_product_caption_is_replaced_with_product_title(self):
        source = {
            "sku": "256",
            "product_name": "",
            "details": "",
            "product_link": "https://www.colourdiam.com/diamonddetails/Diamonds/Product/256",
            "facebook_caption": "Elevate your style with .",
            "hashtags": "#ColourDiam",
            "lang_captions": {},
            "lang_hashtags": {},
        }
        account = {"primary_language": "en-GB", "account_name": "Colour Diam"}
        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("STK 256", caption)
        self.assertNotIn("Elevate your style with .", caption)
        self.assertFalse(main._caption_has_blank_product(caption))

    def test_wrong_diamond_weight_caption_is_replaced(self):
        source = {
            "sku": "681",
            "product_name": "0.11ct Fancy Deep Pink Marquise GIA Natural Diamond",
            "details": "Sku - 681\nWeight - 0.11\nShape - Marquise",
            "product_link": "https://colourdiam.com/Product/Diamond/681/",
            "instagram_caption": "A 0.05 ct Light Bluish Gray Round diamond.",
            "hashtags": "#diamond",
            "lang_captions": {},
            "lang_hashtags": {},
        }
        account = {"primary_language": "en", "account_name": "Colour Diam"}

        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertIn("0.11", caption)
        self.assertNotIn("0.05", caption)
        self.assertNotIn("Round", caption)

    def test_wrong_diamond_shape_caption_is_replaced(self):
        source = {
            "sku": "8717",
            "product_name": "1.21 Ct Light Green Yellow Marquise Diamond VVS2",
            "details": "Sku - 8717\nWeight - 1.21\nColour - Light Green Yellow\nShape - Marquise",
            "product_link": "https://www.colourdiam.com/diamonddetails/Diamonds/Product/8717",
            "lang_captions": {
                "vi": "Viên kim cương tự nhiên 1.21 carat hình hạt lê màu vàng lục nhạt.",
            },
            "lang_hashtags": {},
        }
        account = {
            "primary_language": "vi-VN",
            "account_name": "Colour Diam Vietnam",
        }

        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("1.21", caption)
        self.assertIn("Marquise", caption)
        self.assertNotIn("hạt lê", caption)
        self.assertIn("#KimCương", caption)

    def test_matching_diamond_weight_caption_is_allowed(self):
        source = {
            "sku": "681",
            "product_name": "0.11ct Fancy Deep Pink Marquise GIA Natural Diamond",
            "product_link": "https://colourdiam.com/Product/Diamond/681/",
            "facebook_caption": "A rare 0,11 ct Fancy Deep Pink diamond.",
            "hashtags": "#diamond",
            "lang_captions": {},
            "lang_hashtags": {},
        }
        account = {"primary_language": "en", "account_name": "Colour Diam"}

        caption = main.build_caption({"platform": "facebook"}, source, account)
        self.assertIn("0,11 ct", caption)

    def test_non_diamond_jewellery_copy_is_not_rejected(self):
        source = {
            "sku": "ring-1",
            "product_name": "0.50ct Diamond Ring",
            "product_link": "https://colourdiam.com/Product/Jewellery/ring-1/",
            "instagram_caption": "A ring with two 0.25 ct diamonds.",
            "hashtags": "#jewellery",
            "lang_captions": {},
            "lang_hashtags": {},
        }
        account = {"primary_language": "en", "account_name": "Colour Diam"}

        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertIn("0.25 ct", caption)

    def test_sweden_generates_native_fallback_when_source_cell_is_empty(self):
        source = {
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
            "product_name": "Fancy Yellow Diamond Ring",
        }
        account = {
            "primary_language": "sv-SE",
            "fallback_language": "en-GB",
            "account_name": "Colour Diam Sweden",
        }

        caption = main.build_caption({"platform": "instagram"}, source, account)
        self.assertIn("#Diamanter", caption)
        self.assertIn("Visa produkt:", caption)
        self.assertNotIn("#diamond", caption)
        self.assertNotIn("Missing required sv regional caption", caption)

    def test_empty_regional_source_cells_use_native_fallback_not_english(self):
        source = {
            "lang_captions": {},
            "lang_hashtags": {},
            "hashtags": "#diamond",
            "product_link": "https://colourdiam.com/product/1",
            "product_name": "Fancy Yellow Diamond Ring",
        }
        expected_tags = {
            "de-DE": "#Diamanten",
            "pl-PL": "#Diamenty",
            "da-DK": "#Diamanter",
            "fr-FR": "#Diamants",
            "it-IT": "#Diamanti",
            "es-ES": "#Diamantes",
            "ja-JP": "#ダイヤモンド",
            "ko-KR": "#다이아몬드",
            "vi-VN": "#KimCương",
            "id-ID": "#Berlian",
            "he-IL": "#יהלומים",
            "ar-KW": "#ألماس",
        }
        for language, tag in expected_tags.items():
            account = {
                "primary_language": language,
                "fallback_language": "en-GB",
                "account_name": "Colour Diam",
            }
            caption = main.build_caption({"platform": "facebook"}, source, account)
            self.assertIn(tag, caption, language)
            self.assertNotIn("#diamond", caption)

    def test_all_regional_languages_have_local_product_link_labels(self):
        regional_codes = set(SheetsReader.LANG_CAPTION_COLS) - {"en"}
        self.assertTrue(regional_codes <= set(main._PRODUCT_LINK_LABELS))


if __name__ == "__main__":
    unittest.main()
