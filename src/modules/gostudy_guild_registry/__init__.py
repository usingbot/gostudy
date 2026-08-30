async def setup(bot):
    from .cog import GoStudyGuildRegistryCog

    await bot.add_cog(GoStudyGuildRegistryCog(bot))
