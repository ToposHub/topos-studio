"""
ChannelEvaluatorMixin - Channel manipulation effect implementations.

Provides GPU-based channel effects:
- Shuffle (channel remap/reorder)
- Copy Channel (copy from one texture to another)
- Remove Channels (replace channels with fill value)
- Channel Merge (merge up to 4 inputs into RGBA)

All effects support proper channel indexing (R=0, G=1, B=2, A=3).
"""


from ..nodes.base_node import TextureResult



class ChannelEvaluatorMixin:
    """Mixin providing channel manipulation effect implementations."""

    def _apply_shuffle(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply channel shuffle / remap."""
        prog = self.utility_shaders.get('shuffle')
        if prog is None:
            return {"image": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0
            prog['u_r_from'].value = int(params.get("r_from", 0))
            prog['u_g_from'].value = int(params.get("g_from", 1))
            prog['u_b_from'].value = int(params.get("b_from", 2))
            prog['u_a_from'].value = int(params.get("a_from", 3))
            self._render_quad(prog)
        except Exception:
            # AUD-5930: uniform 直索引/渲染抛错时归还已获取的池纹理。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "image": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_copy_channel(self, input_a: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply copy channel from input B into input A.

        Output canvas/bbox follows input A (the destination) so the rest of
        the graph sees a same-size image with one channel replaced.
        """
        prog = self.utility_shaders.get('copy_channel')
        if prog is None:
            return {"image": input_a}

        input_b = params.get("input_b")
        if not input_b or not input_b.is_valid:
            return {"image": input_a}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_a.texture_id)
            from ..core.gl_bind import bind_texture_unit
            bind_texture_unit(input_b.texture_id, 1)

            prog['u_texture_a'].value = 0
            prog['u_texture_b'].value = 1
            prog['u_src_channel'].value = int(params.get("src_channel", 3))
            prog['u_dst_channel'].value = int(params.get("dst_channel", 3))
            self._render_quad(prog)

            bind_texture_unit(0, 1)
        except Exception:
            # AUD-5930: 出错帧解绑单元 1 并归还已获取的池纹理。
            self._unbind_texture_unit(1)
            self._release_effect_textures_on_error(out_tex)
            raise

        # When the alpha channel is replaced, the output premult state depends
        # on the source of the new alpha; mark as straight so downstream nodes
        # treat the alpha literally.
        dst_channel = int(params.get("dst_channel", 3))
        alpha_premult = False if dst_channel == 3 else input_a.alpha_premultiplied

        return {
            "image": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_a.format,
                bbox=input_a.effective_bbox(),
                alpha_premultiplied=alpha_premult,
                color_space=input_a.color_space,
            )
        }

    def _apply_remove_channels(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply remove_channels: replace specified channels with fill value.

        Output is a fresh texture sized to the input, with the same format/
        bbox/color_space so downstream spatial layout decisions match the
        source. When the alpha channel is removed the output is marked
        straight (alpha replaced by fill_value, no premultiplication).
        """
        prog = self.utility_shaders.get('remove_channels')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0

            remove_r = bool(params.get("remove_r", False))
            remove_g = bool(params.get("remove_g", False))
            remove_b = bool(params.get("remove_b", False))
            remove_a = bool(params.get("remove_a", False))
            fill_value = float(params.get("fill_value", 0.0))

            prog['u_remove_r'].value = remove_r
            prog['u_remove_g'].value = remove_g
            prog['u_remove_b'].value = remove_b
            prog['u_remove_a'].value = remove_a
            prog['u_fill_value'].value = fill_value

            self._render_quad(prog)
        except Exception:
            # AUD-5930: 出错帧归还已获取的池纹理。
            self._release_effect_textures_on_error(out_tex)
            raise

        # When alpha is reset to a constant the RGB/alpha premultiplication
        # relationship no longer holds; mark the result as straight so a
        # subsequent Premult/Unpremult node does the right thing.
        alpha_premult = False if remove_a else input_tex.alpha_premultiplied

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=alpha_premult,
                color_space=input_tex.color_space,
            )
        }

    def _apply_channel_merge(self, params: dict, w: int, h: int) -> dict:
        """Apply channel merge operation.

        Merges up to 4 input textures into a single RGBA output.
        Each output channel can be sourced from any input or a constant value.
        """
        prog = self.utility_shaders.get('channel_merge')
        if prog is None:
            first_valid = next(
                (params.get(f"input_{ch}") for ch in ["r", "g", "b", "a"]
                 if params.get(f"input_{ch}") and params.get(f"input_{ch}").is_valid),
                None
            )
            if first_valid:
                return {"result": first_valid}
            return {}

        input_r = params.get("input_r")
        input_g = params.get("input_g")
        input_b = params.get("input_b")
        input_a = params.get("input_a")

        has_r = input_r is not None and input_r.is_valid
        has_g = input_g is not None and input_g.is_valid
        has_b = input_b is not None and input_b.is_valid
        has_a = input_a is not None and input_a.is_valid

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            from ..core.gl_bind import bind_texture_unit

            # Missing channels bind a cached 1×1 dummy (content gated off by
            # u_has_*; pool textures are never initialized, so a fresh pool
            # texture must not be used as a sampled stand-in).
            dummy = self._get_white_mask_texture().glo

            # Texture unit 0: input_r
            if has_r:
                self._bind_external_texture(input_r.texture_id)
            else:
                self._bind_external_texture(dummy)
            prog['u_texture_r'].value = 0

            # Texture unit 1: input_g
            bind_texture_unit(
                input_g.texture_id if has_g else dummy, 1)
            prog['u_texture_g'].value = 1

            # Texture unit 2: input_b
            bind_texture_unit(
                input_b.texture_id if has_b else dummy, 2)
            prog['u_texture_b'].value = 2

            # Texture unit 3: input_a
            bind_texture_unit(
                input_a.texture_id if has_a else dummy, 3)
            prog['u_texture_a'].value = 3

            prog['u_has_r'].value = has_r
            prog['u_has_g'].value = has_g
            prog['u_has_b'].value = has_b
            prog['u_has_a'].value = has_a

            prog['u_r_source'].value = params.get("r_source", 0)
            prog['u_g_source'].value = params.get("g_source", 1)
            prog['u_b_source'].value = params.get("b_source", 2)
            prog['u_a_source'].value = params.get("a_source", 3)
            prog['u_r_channel'].value = params.get("r_channel", 0)
            prog['u_g_channel'].value = params.get("g_channel", 1)
            prog['u_b_channel'].value = params.get("b_channel", 2)
            prog['u_a_channel'].value = params.get("a_channel", 3)

            prog['u_r_constant'].value = params.get("r_constant", 0.0)
            prog['u_g_constant'].value = params.get("g_constant", 0.0)
            prog['u_b_constant'].value = params.get("b_constant", 0.0)
            prog['u_a_constant'].value = params.get("a_constant", 1.0)

            self._render_quad(prog)

            for i in range(4):
                bind_texture_unit(0, i)
        except Exception:
            # AUD-5930: 出错帧解绑已用纹理单元（0-3 引用池化输入）并
            # 归还已获取的输出纹理。
            for i in range(4):
                self._unbind_texture_unit(i)
            self._release_effect_textures_on_error(out_tex)
            raise

        first_valid_input = next(
            (tex for tex in [input_r, input_g, input_b, input_a]
             if tex and tex.is_valid),
            None
        )

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=first_valid_input.format if first_valid_input else None,
                bbox=first_valid_input.effective_bbox() if first_valid_input else None,
                alpha_premultiplied=first_valid_input.alpha_premultiplied if first_valid_input else None,
                color_space=first_valid_input.color_space if first_valid_input else None,
            )
        }