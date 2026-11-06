# swap91 残件リスト（形式の修正が先の30種）

swap 判定は維持。ここにあるのは「台帳にレグを出すための形式」が未完成の30種で、
今夜の最小アダプター（87形式）には含めていない。修正の受け入れ条件は、
サンプル実測で金額が Transfer と一致すること（Muse の金額突合を通過すること）。

## A. トークンの対応付けのずれ（金額の語は正しい）— 8件
- `0x0e9201911743fd4d03e146f00ad23945dc8f3ffc200906eff25179a52b726f17`（B便） RangoSwap(address,address,address,uint256,uint256,uint16,uint256,address,string)
  - 問題: 金額は両側とも実レシートに厳密一致するが、形式の token_in/out が別の語を指している
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0xe218769062ebfebe8ef494a84976451e605d8f6cc96e5aa495bc2df486a43878
- `0xa98f07efc4d0a837f30fe354a44231615ea1ef631a52a1c83fa7c19b5128dd0f`（B便） Swap(address,address,uint256,uint256,int256,int256,uint256,uint256)
  - 問題: amount_in が別トークンの Transfer と一致し、amount_out が自称 token_in の Transfer と一致（in/out 割当の入替）
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0xa32a4c08aef549162f926a91021f615fe336a430b4397a6418b85ffbc39d5c9a
- `0x365f10e9e7ce45d7acfd986c42e0b666f8af282e440e6dafc78c1f2b2f786760`（B便） HookSwap(bytes32,address,int128,int128,uint128,uint128)
  - 問題: amount_out は別トークンの Transfer と厳密一致。amount_in は未一致
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0x177454580fb65f24f5a34ec01289115fa016912440b8529b35bc457f50a80e14
- `0xb280b2fd9334458c7b5a2343837fec55db58deb4c932372b74fa588e170cce6d`（C便） CoinBuy(address,address,address,uint256,address,uint256,uint256)
  - 問題: 両金額とも厳密一致。ただし自称 token_out は address(0) で、実際の out トークンは別
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0x3087917e78621c4065667f9dc28aec1dcfbba502d00a428d2cb4430dfe9d6250
- `0x9d5b7f31c5338a7ece1b4343740b50239e6bddaff0fc515a8a4a0644088536e1`（C便） Sold(address,uint256,uint256,uint256,address)
  - 問題: 両金額とも厳密一致。token_in の指す語が実際の in トークンと違う
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0x4cf574be870687fd3b1b7c86a4e290a00a9533d7f87fa4fbe7357ceb820ccc77
- `0xdba43ee9916cb156cc32a5d3406e87341e568126a46815294073ba25c9400246`（A便） Swap(address,address,address,address,uint256,uint256,uint256)
  - 問題: amount_out が token_in の Transfer と厳密一致、amount_in はその 1e-9（in/out 入替＋単位）
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0xd24c7dc0e428061c112c047ab304f2eca7b18ac3e75e173b9f85a6763a4ed51e
- `0x8619026a40d38bedb4002fe511cea4bc4a9b336710efe8f21a61869a7ee0f02a`（A便） Swap(address,address,address,uint256,uint256,(uint8,address,address,address,uint24,int24,address,bytes,address,bytes32)[])
  - 問題: amount_out は別トークンの Transfer と厳密一致（自称 token_out は address(0)）。amount_in は送金の 1.01 倍（手数料込みの疑い）
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0x165cf28df82c3a3dcaa6f9c44cedac09e360340faf54bbef34944625ad5a4e3f
- `0x3145c7c5a7804148dd68148a26f9f6a2ad2816be643e0f456290a8b81b9c5154`（A便） Swap(address,address,address,uint256,uint256,(uint8,address,address,address,uint24,int24,address,bytes)[])
  - 問題: amount_out は別トークンの Transfer と厳密一致。amount_in は単位ずれ（1e15 倍の Transfer が存在）
  - 修正: サンプル実測で token_in/token_out の正しい語を特定し直す（金額の語は正しい前提）
  - サンプル: 0xf21c3e04642ca9cc45b787700a03f16687041b986d708a08fda83f01c8dcadc3

## B. 片側のみ不一致（手数料・分割・msgvalue の確定が要る）— 5件
- `0xfa2dda1cc1b86e41239702756b13effbc1a092b5c57e3ad320fbe4f3b13fe235`（B便） Swap(address,address,uint256,uint256)
  - 問題: amount_in は厳密一致。amount_out のみ未一致（手数料・分割の余地）
  - 修正: 未一致側の金額の意味（手数料控除・分割・msgvalue）をサンプルで確定する
  - サンプル: 0x4a3e3a91aa6981cbbf63ccf72675b3494793f773663a8a0b0d10d18e388d052c
- `0x692315744755ee6d80decaeb993d4b1bb95ff5db042cc47b32d5a7d858b01902`（B便） Swapped(address,address,uint256,address,uint256)
  - 問題: amount_in は厳密一致。amount_out のみ未一致
  - 修正: 未一致側の金額の意味（手数料控除・分割・msgvalue）をサンプルで確定する
  - サンプル: 0x061c226acbb6ea0ea2c5d1ae6b4859a84f4bfcf7cc5e2e8e97f0ba070440bf5e
- `0xab5ce61cc1108f6c770a3f1c268c5be3f795d9d805e8a5445a4b480323f5612f`（B便） SimpleSwapped(address,address,address,uint256,uint256,address)
  - 問題: amount_out は厳密一致。amount_in（ネイティブ表示 0xeeee…）が未一致（msgvalue の可能性）
  - 修正: 未一致側の金額の意味（手数料控除・分割・msgvalue）をサンプルで確定する
  - サンプル: 0x590f00be02ecaa3c6838b7e289bf53b955bbba55b637508376f920fb62d224e7
- `0x0fe977d619f8172f7fdbe8bb8928ef80952817d96936509f67d66346bc4cd10f`（B便） Swap(address,address,uint24,bool,uint256,uint256,int24)
  - 問題: amount_out は厳密一致。amount_in は Transfer と近傍だが不一致（相対差 3e-6、手数料差の疑い）
  - 修正: 未一致側の金額の意味（手数料控除・分割・msgvalue）をサンプルで確定する
  - サンプル: 0xc07f6eda3e67140f7b59eb4ea55755df4b6113b2d189bb56e1df2aa54a55850e
- `0xad671c9d50262b75ba17bdf7e330ae0d7da971800b2526584a85f83d23296b15`（C便） SwapExecuted(address,address,address,uint256,uint256,uint256)
  - 問題: amount_out は厳密一致。amount_in は 0.1 ETH のネイティブ（msgvalue 候補で照合不能だった）
  - 修正: 未一致側の金額の意味（手数料控除・分割・msgvalue）をサンプルで確定する
  - サンプル: 0x21922802df4b1f0a4b1ba57cf707409f6b1e90abc5942e6e4ed3f1788d3c3b60

## C. 単位・意味のずれ — 4件
- `0xc528cda9e500228b16ce84fadae290d9a49aecb17483110004c5af0a07f6fd73`（A便） Swap(address,address,uint256,bool,uint256,uint256,uint256,uint256)
  - 問題: 両金額とも Transfer の約 1/100・桁違い（単位ずれの疑い）
  - 修正: 金額の単位・縮尺（decimals 差・パック語）を特定する
  - サンプル: 0xa0f5a582a3dc11c5480f6954fda7a2f351e593a144d95dca6aed1a4571dbfbf0
- `0xad7d6f97abf51ce18e17a38f4d70e975be9c0708474987bb3e26ad21bd93ca70`（B便） Swap(address,address,uint24,bytes32,bytes32,uint24,bytes32,bytes32)
  - 問題: amount_out は厳密一致。amount_in の語は金額ではない（パック語・ハッシュの疑い）—形式側の誤りの可能性が高い
  - 修正: 金額の単位・縮尺（decimals 差・パック語）を特定する
  - サンプル: 0x30fd63eb59ecd69ff6cfeeb0b0afa892bc96087f373a5afa47e3bc647bc02610
- `0x627892cdf8066c17932571d7916884f22f3b911facbe7b6f37e8877c58242105`（A便） TokensSwapped(address,address,uint256[],uint256[],uint256[],uint256[],uint256[],uint256[])
  - 問題: 両側とも未一致。サンプル内の送金が少なく、金額の出所が不明
  - 修正: 金額の単位・縮尺（decimals 差・パック語）を特定する
  - サンプル: 0xbc67bcac082dcd1ad92a3c6d16751a31f7efadc5944fbd9432ba6cd9fa50c631
- `0x76af224a143865a50b41496e1a73622698692c565c1214bc862f18e22d829c5e`（B便） Swapped(address,address,address,address,uint256,uint256,uint256,uint256,uint256,address)
  - 問題: amount_in は厳密一致。amount_out は in と同額の計上（1:1 ラップ会計型）で実送金と一致しない
  - 修正: 金額の単位・縮尺（decimals 差・パック語）を特定する
  - サンプル: 0x27ce2010bf44cf0c0ef497fb261ea9449f2913e60155b46e31ae95bb0d3be039

## D. 未検証（金額フィールドの意味付けが無い）— 9件
- `0xfd53b5d2be8149b740c4dfbcef47be8d9238845e12dde6237a24765a2cbf1b5d`（A便） Swap(bytes32,uint8,address,address,address,uint256,uint256,uint256,int256,uint256)
  - 問題: 照合不能だった（形式の意味付け: ['other:id', 'other:dex', 'other:swapper', 'token_in', 'token_out', 'amount_in', 'amount_out', 'other:minAmountOut', 'other:tier', 'other:deadline']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
- `0x68d2d9ac11f4244eb2dd3ff0c821057cdfd46c8fbd10af187298a53334315bac`（B便） FlashSwapHandled(bytes32,address,address,address,address,address,uint256,uint256,uint256,uint256)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'token_in', 'token_out', 'other', 'other', 'other', 'amount_in', 'amount_out', 'fee', 'fee']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
  - サンプル: 0x677e01a0db028c24cfddf51aee998344c20fcb4ec1c6dbe355fa98319623e1ca
- `0x35001030bac7516ed5b1daf42bbfd3eaf587acc70c90f7f00ee42d5a34d75233`（B便） PoolSwap(bytes32,int256,int256,int256,int256,int256,int256,int256,int256,int256,int256,int256,int256)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'amount_in', 'amount_out', 'other', 'other', 'other', 'other', 'other', 'other', 'other', 'other', 'other', 'other']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
  - サンプル: 0xc9f3a949666c5d1406857147b30962b9a71092e078513b630388e602766099c0
- `0xbf511038cfa32cfb7971e976074c0e6aeda010e6afcbb38d87b4f91030501494`（B便） Swapped(bool,int256,int256)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'amount_in', 'amount_out']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
  - サンプル: 0x5b85e054b1f649bf4494191a6be17f352125bb7694cc0f3ec56a5c378909dbba
- `0xbfd50a04f1e6e4aee344f5d0e7f15d74d0dbb58cd1f711daa6463094ca9508cd`（B便） Swap(address,bool,uint256,uint256)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'other', 'amount_in', 'amount_out']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
  - サンプル: 0xd91cdf5a9f31ae5dc83f125a83705ee038ef050dbd37e0c451610e74b3447990
- `0xb52233096efdadd1f841ef42312c9d50e3d7e1fee8f5bd58c34336b6376ffdd2`（C便） SwapExecuted(address,address,address,address,address,uint256,uint256,uint256,address,uint256,string)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'token_in', 'token_out', 'other', 'other', 'amount_in', 'amount_out', 'fee', 'other', 'fee', 'other', 'other', 'other']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
- `0xea9d2d31b34087ee14504ff4c6722566e9163aff8cc95f6e280dc6f4e08514d4`（C便） Swapped((address,address,address,address,address,uint24,uint256,uint256,uint256,uint256))
  - 問題: 照合不能だった（形式の意味付け: ['other', 'other', 'other', 'token_in', 'token_out', 'fee', 'amount_in', 'amount_out', 'other', 'other']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
- `0x881cb06ae984d9e71db210f224332d1183c2b2c30010e4aea15ef8bf02901932`（C便） Swap(uint8,uint256,uint256)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'amount_in', 'amount_out']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する
- `0xb5e64f967a9524d6a50602ad4833db0fde476f580ac0fe92952de19573884a69`（C便） SplitSwapExecuted(address,address,address,uint256,uint256,uint256,uint256)
  - 問題: 照合不能だった（形式の意味付け: ['other', 'token_in', 'token_out', 'amount_in', 'amount_out', 'amount_out', 'fee']）
  - 修正: 金額フィールドの意味付け（amount_in/out への対応）をサンプルで確定する

## E. 手作業トレース要 — 2件
- `0xf23fb97e8642200d71532c9c5ae884a9236ca6b8eb58fed071a7cc3f2ac4161d`（A便） Swap(address,address,address[],uint256,uint256,uint8,uint256,address)
  - 問題: 複数イベント・複数送金が絡み自動照合で決着せず。手作業トレース要
  - 修正: サンプル tx を手で全レグ追跡して金額の対応を確定する
  - サンプル: 0xc86f7fc0bc938526de19378fafe81fdf98ce685fb9edbb901918f042e4bed998
- `0x6af07fec27a8c155131e72c96138ede7f9087476d47e469a69839c8bf3261e4a`（C便） Swapped(uint256,uint256,uint160,uint160)
  - 問題: 金額が送金の合計に近いが厳密一致しない（約 0.6% 差）。手作業トレース要
  - 修正: サンプル tx を手で全レグ追跡して金額の対応を確定する
  - サンプル: 0x5d556be536121f9af06aa2ac2d9c9e0906341290b3e6ad1cac77dfb176ce86d2

## F. 多形性（発行元別の形式が要る）— 1件
- `0x4d3257edb95914b701655f9bf4b58e4b422017a7b94dad03a20dae00f4b1cbb7`（C便） SwapExecuted(uint256,uint256,uint256)
  - 問題: 発行元で形が違う（支配的発行元は indexed 1、パックのサンプルは indexed 0）。発行元別の形式が要る
  - 修正: 発行元ごとに形式を分けて記録する
  - サンプル: 0x0fbc58dd16b22b748b53a3912a903cf9d5936a5ac9e1811255b4568978665739

## G. 形式なし — 1件
- `0x9bac37bc1cd52b4c1682c957aeea74388700042376fcfec42cccfc69d73819e8`（C便） FallbackSwap(address,address,address,uint256,uint8,uint8)
  - 問題: swap 判定だが形式が成果物に含まれていなかった
  - 修正: サンプルと署名から形式を導出する
